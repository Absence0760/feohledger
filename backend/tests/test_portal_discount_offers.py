"""Supplier-portal early-payment discount offers — vendor-scoped list + accept.

DB-backed (the `realdb` fixture) because the security-critical behaviour here is
data-layer: a vendor must only ever see offers scoped to their own vendor_id (or
their own invoices), accepting an offer only flips its status (never mints a
Payment/PaymentRun), and a double-accept is a safe 409 with no double-count.
Source-level mocks can't prove the accept never seeds the money path — only a
real round-trip can.

Mirrors the threading style of `test_portal_self_service.py`: one tenant
sessionmaker per test, a vendor JWT client over the realdb ASGI app.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.deps import create_vendor_access_token
from app.models.discount import (
    OFFER_SCOPE_INVOICE,
    OFFER_SCOPE_VENDOR,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog
from app.utils.dates import utc_today

TENANT = "a"

_TIERS = [{"days": 5, "percent": "3.00"}, {"days": 10, "percent": "2.00"}]


async def _seed_vendor_and_user(mk, org_id, *, name="Acme Supply") -> tuple[uuid.UUID, uuid.UUID]:
    vendor_id = uuid.uuid4()
    vu_id = uuid.uuid4()
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
            )
        )
        await s.commit()
    return vendor_id, vu_id


_UNSET = object()


async def _seed_vendor_offer(
    mk,
    org_id,
    vendor_id,
    *,
    base_amount="10000.00",
    status=OFFER_STATUS_OFFERED,
    tiers=None,
    valid_from=_UNSET,
    valid_until=_UNSET,
) -> uuid.UUID:
    """A vendor-scoped offer (scope=vendor) belonging to `vendor_id`."""
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
                tiers=tiers if tiers is not None else _TIERS,
                base_amount=Decimal(base_amount),
                currency="USD",
                valid_from=(utc_today() if valid_from is _UNSET else valid_from),
                valid_until=(
                    utc_today() + timedelta(days=30) if valid_until is _UNSET else valid_until
                ),
            )
        )
        await s.commit()
    return offer_id


async def _seed_invoice_offer(
    mk, org_id, vendor_id, *, base_amount="2500.00"
) -> tuple[uuid.UUID, uuid.UUID]:
    """An invoice-scoped offer on one of `vendor_id`'s invoices."""
    invoice_id = uuid.uuid4()
    offer_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=invoice_id,
                invoice_number="INV-DISC-1",
                vendor_name="Acme Supply",
                vendor_id=vendor_id,
                amount=Decimal(base_amount),
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
            )
        )
        # Flush the invoice before the offer so the offer's invoice_id FK resolves
        # (SQLAlchemy won't infer the insert order without a relationship).
        await s.flush()
        s.add(
            DiscountOffer(
                id=offer_id,
                organization_id=org_id,
                scope=OFFER_SCOPE_INVOICE,
                invoice_id=invoice_id,
                source="supplier",
                status=OFFER_STATUS_OFFERED,
                tiers=_TIERS,
                base_amount=Decimal(base_amount),
                currency="USD",
                valid_from=utc_today(),
                valid_until=utc_today() + timedelta(days=30),
            )
        )
        await s.commit()
    return offer_id, invoice_id


def _portal_client(realdb, vendor_user_id: uuid.UUID, vendor_id: uuid.UUID):
    token = create_vendor_access_token(vendor_user_id, vendor_id)
    client = realdb.client(key=TENANT, role=None)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# List — vendor scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_vendor_scoped_offers_with_savings(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    await _seed_vendor_offer(mk, org_id, vendor_id, base_amount="10000.00")
    await _seed_invoice_offer(mk, org_id, vendor_id, base_amount="2500.00")

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get("/api/portal/discount-offers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    # Both the vendor-scoped and the invoice-scoped (own invoice) offer show up.
    scopes = {o["scope"] for o in body["items"]}
    assert scopes == {OFFER_SCOPE_VENDOR, OFFER_SCOPE_INVOICE}

    by_scope = {o["scope"]: o for o in body["items"]}
    vendor_offer = by_scope[OFFER_SCOPE_VENDOR]
    # base 10000 * 3% = 300.00 best-tier savings; tiers carry per-rung savings.
    assert vendor_offer["best_tier"]["percent"] == 3.0
    assert vendor_offer["best_tier"]["savings"] == 300.0
    tier_savings = {t["days"]: t["savings"] for t in vendor_offer["tiers"]}
    assert tier_savings == {5: 300.0, 10: 200.0}

    inv_offer = by_scope[OFFER_SCOPE_INVOICE]
    assert inv_offer["invoice_number"] == "INV-DISC-1"


@pytest.mark.asyncio
async def test_list_never_shows_another_vendors_offers(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    mine_vid, mine_vu = await _seed_vendor_and_user(mk, org_id, name="Mine")
    other_vid, _ = await _seed_vendor_and_user(mk, org_id, name="Other")
    await _seed_vendor_offer(mk, org_id, mine_vid)
    await _seed_vendor_offer(mk, org_id, other_vid)

    async with _portal_client(realdb, mine_vu, mine_vid) as client:
        resp = await client.get("/api/portal/discount-offers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["scope"] == OFFER_SCOPE_VENDOR


@pytest.mark.asyncio
async def test_list_requires_vendor_auth(realdb):
    """No vendor JWT → 401 (auth before everything)."""
    async with realdb.client(key=TENANT, role=None) as client:
        resp = await client.get("/api/portal/discount-offers")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_status_filter(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    await _seed_vendor_offer(mk, org_id, vendor_id, status=OFFER_STATUS_OFFERED)
    await _seed_vendor_offer(mk, org_id, vendor_id, status=OFFER_STATUS_ACCEPTED)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get("/api/portal/discount-offers?status=offered")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == OFFER_STATUS_OFFERED


# ---------------------------------------------------------------------------
# Accept — money-path boundary + idempotency + cross-vendor 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flips_status_without_moving_money(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id, base_amount="10000.00")

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/discount-offers/{offer_id}/accept", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == OFFER_STATUS_ACCEPTED
    # Best tier today (5 days → 3%) is chosen when no tier_days is given.
    assert body["accepted_tier"]["days"] == 5
    assert body["accepted_tier"]["percent"] == 3.0
    assert body["accepted_tier"]["savings"] == 300.0

    async with mk() as s:
        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == offer_id))
        ).scalar_one()
        assert offer.status == OFFER_STATUS_ACCEPTED
        assert offer.accepted_tier["percent"] == "3.00"
        # Money-path boundary: accepting created NO Payment / PaymentRun.
        pay_count = (await s.execute(select(func.count()).select_from(Payment))).scalar()
        run_count = (await s.execute(select(func.count()).select_from(PaymentRun))).scalar()
        assert pay_count == 0
        assert run_count == 0
        # Audit row written, PII-free (no value, only the tier).
        actions = {r.action for r in (await s.execute(select(AuditLog))).scalars().all()}
    assert "discount_offer.accepted_by_vendor" in actions


@pytest.mark.asyncio
async def test_accept_specific_tier(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id, base_amount="10000.00")

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            f"/api/portal/discount-offers/{offer_id}/accept", json={"tier_days": 10}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted_tier"]["days"] == 10
    assert body["accepted_tier"]["savings"] == 200.0


@pytest.mark.asyncio
async def test_accept_refuses_a_tier_whose_window_has_closed(realdb):
    """Reproduces issue #124's exact repro: an offer opened 20 days ago with
    a 5-day/3% and 10-day/2% sliding scale, still within its own valid_until
    (+10 days out). Both tiers' real deadlines (measured from when the offer
    was extended) are long past. Before the fix this returned 200 with the
    3% tier — every tier looked perpetually achievable because the deadline
    was measured from "today" instead of from the offer's start."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    offer_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            DiscountOffer(
                id=offer_id,
                organization_id=org_id,
                scope=OFFER_SCOPE_VENDOR,
                vendor_id=vendor_id,
                source="supplier",
                status=OFFER_STATUS_OFFERED,
                tiers=_TIERS,
                base_amount=Decimal("10000.00"),
                currency="USD",
                valid_from=utc_today() - timedelta(days=20),
                valid_until=utc_today() + timedelta(days=10),
            )
        )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/discount-offers/{offer_id}/accept", json={})
    assert resp.status_code == 409, resp.text

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        # Naming the (long-closed) 5-day tier explicitly must not bypass the
        # window check either.
        resp2 = await client.post(
            f"/api/portal/discount-offers/{offer_id}/accept", json={"tier_days": 5}
        )
    assert resp2.status_code == 422, resp2.text

    async with mk() as s:
        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == offer_id))
        ).scalar_one()
        assert offer.status == OFFER_STATUS_OFFERED  # untouched — never accepted


@pytest.mark.asyncio
async def test_double_accept_is_safe_409(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        first = await client.post(f"/api/portal/discount-offers/{offer_id}/accept", json={})
        second = await client.post(f"/api/portal/discount-offers/{offer_id}/accept", json={})
    assert first.status_code == 200
    assert second.status_code == 409

    # Still exactly one accept audit row — no double count.
    async with mk() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "discount_offer.accepted_by_vendor")
            )
        ).scalar()
    assert n == 1


@pytest.mark.asyncio
async def test_accept_foreign_offer_404(realdb):
    """Accepting another vendor's offer is a 404 (never 403), and never mutates
    it."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    mine_vid, mine_vu = await _seed_vendor_and_user(mk, org_id, name="Mine")
    other_vid, _ = await _seed_vendor_and_user(mk, org_id, name="Other")
    foreign_offer = await _seed_vendor_offer(mk, org_id, other_vid)

    async with _portal_client(realdb, mine_vu, mine_vid) as client:
        resp = await client.post(f"/api/portal/discount-offers/{foreign_offer}/accept", json={})
    assert resp.status_code == 404

    async with mk() as s:
        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == foreign_offer))
        ).scalar_one()
        assert offer.status == OFFER_STATUS_OFFERED  # untouched


@pytest.mark.asyncio
async def test_accept_unknown_offer_404(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/discount-offers/{uuid.uuid4()}/accept", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_accept_bad_tier_days_422(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            f"/api/portal/discount-offers/{offer_id}/accept", json={"tier_days": 99}
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Decline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decline_flips_status(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/discount-offers/{offer_id}/decline")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "declined"

    async with mk() as s:
        actions = {r.action for r in (await s.execute(select(AuditLog))).scalars().all()}
    assert "discount_offer.declined_by_vendor" in actions


@pytest.mark.asyncio
async def test_decline_foreign_offer_404(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    mine_vid, mine_vu = await _seed_vendor_and_user(mk, org_id, name="Mine")
    other_vid, _ = await _seed_vendor_and_user(mk, org_id, name="Other")
    foreign_offer = await _seed_vendor_offer(mk, org_id, other_vid)

    async with _portal_client(realdb, mine_vu, mine_vid) as client:
        resp = await client.post(f"/api/portal/discount-offers/{foreign_offer}/decline")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# A lapsed offer — the supplier and AP must never see it differently
# ---------------------------------------------------------------------------
#
# `api/discounts` derives an offer's status from `valid_until` rather than
# reading the column, because the column's only writer is the auto-capture
# sweep and that sweep is off by default. The portal rendered the raw column,
# so the same offer read `expired` to AP and `offered` to the supplier — who
# could still act on it. These pin both halves of that split-brain shut.


def _lapsed(**kw):
    kw.setdefault("valid_from", utc_today() - timedelta(days=60))
    kw.setdefault("valid_until", utc_today() - timedelta(days=1))
    return kw


@pytest.mark.asyncio
async def test_lapsed_offer_reads_expired_to_the_supplier(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    lapsed_id = await _seed_vendor_offer(mk, org_id, vendor_id, **_lapsed())
    live_id = await _seed_vendor_offer(mk, org_id, vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        rows = {
            o["id"]: o for o in (await client.get("/api/portal/discount-offers")).json()["items"]
        }
    assert rows[str(lapsed_id)]["status"] == "expired"
    # No headline "accept this" number on a dead offer.
    assert rows[str(lapsed_id)]["best_tier"] is None
    assert rows[str(live_id)]["status"] == OFFER_STATUS_OFFERED
    assert rows[str(live_id)]["best_tier"] is not None

    # Derived, never written — a supplier READ must not mutate the row.
    async with mk() as s:
        row = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == lapsed_id))
        ).scalar_one()
        assert row.status == OFFER_STATUS_OFFERED


@pytest.mark.asyncio
async def test_status_filter_uses_the_effective_status(realdb):
    """`?status=offered` must not hand back an offer whose window closed."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    lapsed_id = await _seed_vendor_offer(mk, org_id, vendor_id, **_lapsed())
    live_id = await _seed_vendor_offer(mk, org_id, vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        open_body = (await client.get("/api/portal/discount-offers?status=offered")).json()
        expired_body = (await client.get("/api/portal/discount-offers?status=expired")).json()

    assert [o["id"] for o in open_body["items"]] == [str(live_id)]
    assert open_body["total"] == 1
    assert [o["id"] for o in expired_body["items"]] == [str(lapsed_id)]
    assert expired_body["total"] == 1


@pytest.mark.asyncio
async def test_portal_summary_does_not_count_a_lapsed_offer_as_open(realdb):
    """The dashboard KPI sits above the same list, so it must count the same
    population — telling a supplier they have two offers to act on when one
    lapsed a month ago is the same split-brain one level up."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    await _seed_vendor_offer(mk, org_id, vendor_id, **_lapsed())
    await _seed_vendor_offer(mk, org_id, vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        summary = (await client.get("/api/portal/summary")).json()
    assert summary["open_discount_offers"] == 1


@pytest.mark.asyncio
async def test_accept_refuses_a_lapsed_offer(realdb):
    """Accept already refuses one — no tier is capturable past `valid_until` —
    and that must stay true, because it is the only thing stopping a supplier
    from claiming a discount the calendar closed. Pinned explicitly rather than
    left as a side effect of the tier-window rule."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id, **_lapsed())

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        best = await client.post(f"/api/portal/discount-offers/{offer_id}/accept")
        named = await client.post(
            f"/api/portal/discount-offers/{offer_id}/accept", json={"tier_days": 5}
        )
    assert best.status_code == 409, best.text
    # Naming a rung explicitly must not get round the window either.
    assert named.status_code == 422, named.text

    async with mk() as s:
        row = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == offer_id))
        ).scalar_one()
        assert row.status == OFFER_STATUS_OFFERED
        assert row.accepted_tier is None
        assert (await s.execute(select(func.count()).select_from(Payment))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(PaymentRun))).scalar_one() == 0


@pytest.mark.asyncio
async def test_decline_refuses_a_lapsed_offer(realdb):
    """Decline was the one path that could still write a decision onto a dead
    offer: it checked only the stored status, so a lapsed offer became
    `declined` — asserting a supplier refusal that never happened, on an
    append-only audit row. It now 409s, and the row is untouched."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id, **_lapsed())

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/discount-offers/{offer_id}/decline")
    assert resp.status_code == 409, resp.text

    async with mk() as s:
        row = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == offer_id))
        ).scalar_one()
        assert row.status == OFFER_STATUS_OFFERED  # not rewritten as `declined`
        actions = {r.action for r in (await s.execute(select(AuditLog))).scalars().all()}
        assert "discount_offer.declined_by_vendor" not in actions


@pytest.mark.asyncio
async def test_decline_still_works_on_the_last_day_of_the_window(realdb):
    """The guard is `valid_until < today`, so the window's own last day is still
    live — it must not eat a legitimate decline a day early."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    offer_id = await _seed_vendor_offer(mk, org_id, vendor_id, valid_until=utc_today())

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/discount-offers/{offer_id}/decline")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "declined"
