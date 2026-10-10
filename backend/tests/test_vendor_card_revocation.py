"""A vendor that stops being payable must not keep a spendable virtual card.

Payment runs and `POST /api/cards/generate` refuse a vendor that is not
`active` or is `payments_blocked`, but those gates only stop the NEXT card. A
card minted while the vendor was payable stayed live after the vendor was
rejected, deactivated, blocked or sanctions-matched. Every such write now runs
`services/vendor_card_revocation.revoke_vendor_cards` in its own transaction.

Covered here, against a live Postgres so the row locks, the audit trail and the
commit are the real ones:

  * every door — PATCH status, `/reject`, `/bulk/status`, `/block`, a sanctions
    match via `/screen`, and a consolidation merge onto an un-payable canonical;
  * which cards are touched: live + unbooked only — spent / expired / already
    cancelled cards are left alone, and a card behind a LIVE payment is
    reported `requires_payment_void` (that is the void's job), while a card
    behind a voided payment is cancelled;
  * a provider failure fails VISIBLY: the card stays live, the status change
    still commits, the response names the card, a `card.cancel_failed` row is
    written — and `POST /{id}/cancel-cards` finishes the job.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.virtual_card import VirtualCard
from app.models.workflow import AuditLog

TENANT = "a"

_MOCK_CANCEL = "app.services.card_adapters.mock_adapter.MockCardAdapter.cancel_card"


@pytest.fixture
def mk(realdb):
    return realdb.sessionmaker(TENANT)


@pytest.fixture
async def cards_enabled(realdb):
    """Route the org at the in-process mock card adapter (no credential)."""
    org_id = realdb.info(TENANT).org_id
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        settings["cards"] = {"enabled": True, "program_type": "byok", "provider": "mock"}
        org.settings = settings
        await s.commit()
    return org_id


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed_vendor(mk, org_id, *, name="Card Vendor Co", status="active") -> uuid.UUID:
    async with mk() as s:
        ent = await _default_entity_id(s)
        v = Vendor(organization_id=org_id, entity_id=ent, name=name, status=status)
        s.add(v)
        await s.commit()
        return v.id


async def _seed_card(
    mk,
    org_id,
    vendor_id,
    *,
    status: str = "created",
    payment_status: str | None = None,
) -> uuid.UUID:
    """One invoice + its card (and, with `payment_status`, the payment it
    settles). Each card gets its own invoice — one live card per invoice."""
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"REVOKE-{uuid.uuid4().hex[:8]}",
            vendor_name="Card Vendor Co",
            vendor_id=vendor_id,
            amount=Decimal("125.00"),
            currency="USD",
            status=InvoiceStatus.approved,
            correlation_id=uuid.uuid4(),
        )
        s.add(inv)
        await s.flush()
        payment_id = None
        if payment_status is not None:
            run = PaymentRun(
                organization_id=org_id,
                status="submitted",
                total_amount=Decimal("125.00"),
                requires_cfo_approval=False,
            )
            s.add(run)
            await s.flush()
            pay = Payment(
                invoice_id=inv.id,
                payment_run_id=run.id,
                amount=Decimal("125.00"),
                method="virtual_card",
                status=payment_status,
            )
            s.add(pay)
            await s.flush()
            payment_id = pay.id
        card = VirtualCard(
            invoice_id=inv.id,
            payment_id=payment_id,
            vendor_id=vendor_id,
            organization_id=org_id,
            entity_id=ent,
            correlation_id=inv.correlation_id,
            card_provider="mock",
            provider_card_id=f"mock_{uuid.uuid4().hex[:10]}",
            last_four="4242",
            amount_limit=Decimal("125.00"),
            currency="USD",
            status=status,
        )
        s.add(card)
        await s.commit()
        return card.id


async def _card_status(mk, card_id) -> str:
    async with mk() as s:
        return (await s.get(VirtualCard, card_id)).status


async def _audit(mk, card_id, action) -> list[AuditLog]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.entity_id == card_id, AuditLog.action == action)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------


async def test_reject_cancels_the_vendors_live_card_with_an_audit_row(realdb, mk, cards_enabled):
    org_id = cards_enabled
    vid = await _seed_vendor(mk, org_id)
    card_id = await _seed_card(mk, org_id, vid)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/{vid}/reject")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["card_revocation"] == {
        "vendor_id": str(vid),
        "cancelled": 1,
        "not_closed": [],
        "requires_payment_void": [],
    }

    assert await _card_status(mk, card_id) == "cancelled"
    rows = await _audit(mk, card_id, "card.cancelled")
    assert len(rows) == 1
    assert rows[0].details["trigger"] == "vendor.rejected"
    assert rows[0].details["via"] == "vendor_ineligible"
    assert rows[0].details["vendor_id"] == str(vid)
    assert rows[0].details["from"] == "created"


async def test_patch_status_inactive_cancels_but_an_unrelated_edit_does_not(
    realdb, mk, cards_enabled
):
    org_id = cards_enabled
    vid = await _seed_vendor(mk, org_id)
    card_id = await _seed_card(mk, org_id, vid)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.patch(f"/api/vendors/{vid}", json={"phone": "555-0100"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["card_revocation"] is None
        assert await _card_status(mk, card_id) == "created"

        resp = await c.patch(f"/api/vendors/{vid}", json={"status": "inactive"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["card_revocation"]["cancelled"] == 1
    assert await _card_status(mk, card_id) == "cancelled"
    rows = await _audit(mk, card_id, "card.cancelled")
    assert [r.details["trigger"] for r in rows] == ["vendor.status_changed"]


async def test_block_cancels_the_live_card(realdb, mk, cards_enabled):
    org_id = cards_enabled
    vid = await _seed_vendor(mk, org_id)
    card_id = await _seed_card(mk, org_id, vid, status="active")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/{vid}/block", json={"reason": "fraud review"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["payments_blocked"] is True
    assert resp.json()["card_revocation"]["cancelled"] == 1
    assert await _card_status(mk, card_id) == "cancelled"
    rows = await _audit(mk, card_id, "card.cancelled")
    assert rows[0].details["trigger"] == "vendor.payment_blocked"


async def test_bulk_reject_reports_per_vendor_revocations(realdb, mk, cards_enabled):
    org_id = cards_enabled
    with_card = await _seed_vendor(mk, org_id, name="Has Card")
    without_card = await _seed_vendor(mk, org_id, name="No Card")
    card_id = await _seed_card(mk, org_id, with_card)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/vendors/bulk/status",
            json={"ids": [str(with_card), str(without_card)], "status": "rejected"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 2
    # Only a vendor that actually held a card is listed.
    assert [r["vendor_id"] for r in body["card_revocations"]] == [str(with_card)]
    assert body["card_revocations"][0]["cancelled"] == 1
    assert await _card_status(mk, card_id) == "cancelled"


async def test_sanctions_match_on_screen_cancels_the_live_card(realdb, mk, cards_enabled):
    org_id = cards_enabled
    # The default mock sanctions adapter always matches this fixture name.
    vid = await _seed_vendor(mk, org_id, name="Sanctioned Test Entity")
    card_id = await _seed_card(mk, org_id, vid)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/{vid}/screen")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["payments_blocked"] is True
    assert body["card_revocation"]["cancelled"] == 1
    assert await _card_status(mk, card_id) == "cancelled"
    rows = await _audit(mk, card_id, "card.cancelled")
    assert rows[0].details["trigger"] == "vendor.sanctions_match"


async def test_merge_onto_an_unpayable_canonical_cancels_the_inherited_card(
    realdb, mk, cards_enabled
):
    """The merge re-homes the duplicate's card onto the canonical; when the
    canonical is itself un-payable that card must not stay spendable."""
    org_id = cards_enabled
    canonical = await _seed_vendor(mk, org_id, name="Canonical Co", status="inactive")
    duplicate = await _seed_vendor(mk, org_id, name="Canonical Co Dup")
    card_id = await _seed_card(mk, org_id, duplicate)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/enrichment/vendors/consolidation/merge",
            json={"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(duplicate)]},
        )
    assert resp.status_code == 200, resp.text
    revs = resp.json()["card_revocations"]
    assert [r["vendor_id"] for r in revs] == [str(canonical)]
    assert revs[0]["cancelled"] == 1
    async with mk() as s:
        card = await s.get(VirtualCard, card_id)
        assert card.vendor_id == canonical
        assert card.status == "cancelled"


async def test_merge_onto_a_payable_canonical_leaves_the_card_live(realdb, mk, cards_enabled):
    org_id = cards_enabled
    canonical = await _seed_vendor(mk, org_id, name="Keep Co")
    duplicate = await _seed_vendor(mk, org_id, name="Keep Co Dup")
    card_id = await _seed_card(mk, org_id, duplicate)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/enrichment/vendors/consolidation/merge",
            json={"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(duplicate)]},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["card_revocations"] == []
    assert await _card_status(mk, card_id) == "created"


# ---------------------------------------------------------------------------
# Which cards
# ---------------------------------------------------------------------------


async def test_only_live_unbooked_cards_are_cancelled(realdb, mk, cards_enabled):
    org_id = cards_enabled
    vid = await _seed_vendor(mk, org_id)
    unbooked = await _seed_card(mk, org_id, vid)
    behind_voided = await _seed_card(mk, org_id, vid, payment_status="voided")
    behind_live = await _seed_card(mk, org_id, vid, payment_status="completed")
    spent = await _seed_card(mk, org_id, vid, status="charged")
    expired = await _seed_card(mk, org_id, vid, status="expired")
    already = await _seed_card(mk, org_id, vid, status="cancelled")

    cancel = AsyncMock(return_value=True)
    with patch(_MOCK_CANCEL, cancel):
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/vendors/{vid}/reject")
    assert resp.status_code == 200, resp.text
    rev = resp.json()["card_revocation"]
    assert rev["cancelled"] == 2
    assert rev["not_closed"] == []
    assert len(rev["requires_payment_void"]) == 1
    assert rev["requires_payment_void"][0]["card_id"] == str(behind_live)
    assert rev["requires_payment_void"][0]["outcome"] == "payment_live"
    assert rev["requires_payment_void"][0]["payment_id"] is not None

    # The provider was asked about exactly the two unbooked cards.
    assert cancel.await_count == 2

    assert await _card_status(mk, unbooked) == "cancelled"
    assert await _card_status(mk, behind_voided) == "cancelled"
    # Booked money is the void's to close — still live, and audited as deferred.
    assert await _card_status(mk, behind_live) == "created"
    deferred = await _audit(mk, behind_live, "card.cancel_deferred_to_void")
    assert len(deferred) == 1
    assert deferred[0].details["payment_id"] == rev["requires_payment_void"][0]["payment_id"]
    # Dead cards are untouched and unaudited.
    for card_id, st in ((spent, "charged"), (expired, "expired"), (already, "cancelled")):
        assert await _card_status(mk, card_id) == st
        assert await _audit(mk, card_id, "card.cancelled") == []


async def test_another_vendors_card_is_never_touched(realdb, mk, cards_enabled):
    org_id = cards_enabled
    rejected = await _seed_vendor(mk, org_id, name="Going Away")
    other = await _seed_vendor(mk, org_id, name="Staying")
    other_card = await _seed_card(mk, org_id, other)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/{rejected}/reject")
    assert resp.status_code == 200, resp.text
    assert resp.json()["card_revocation"]["cancelled"] == 0
    assert await _card_status(mk, other_card) == "created"


# ---------------------------------------------------------------------------
# Provider failure fails visibly, and the retry finishes it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cancel_mock", "outcome"),
    [
        (AsyncMock(return_value=False), "card_cancel_rejected"),
        (AsyncMock(side_effect=TimeoutError("provider down")), "card_cancel_error:TimeoutError"),
    ],
)
async def test_provider_failure_leaves_card_live_and_reports_it(
    realdb, mk, cards_enabled, cancel_mock, outcome
):
    org_id = cards_enabled
    vid = await _seed_vendor(mk, org_id)
    card_id = await _seed_card(mk, org_id, vid)

    with patch(_MOCK_CANCEL, cancel_mock):
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/vendors/{vid}/reject")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The defensive status change is not held hostage by the card processor.
    assert body["status"] == "rejected"
    async with mk() as s:
        assert (await s.get(Vendor, vid)).status == "rejected"
    # ...but the live card is named, never reported as handled.
    rev = body["card_revocation"]
    assert rev["cancelled"] == 0
    assert rev["not_closed"] == [
        {"card_id": str(card_id), "last_four": "4242", "outcome": outcome, "payment_id": None}
    ]
    assert await _card_status(mk, card_id) == "created"
    failed = await _audit(mk, card_id, "card.cancel_failed")
    assert len(failed) == 1
    assert failed[0].details["outcome"] == outcome
    assert await _audit(mk, card_id, "card.cancelled") == []

    # The retry exit closes it once the provider answers.
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/{vid}/cancel-cards")
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled"] == 1
    assert resp.json()["not_closed"] == []
    assert await _card_status(mk, card_id) == "cancelled"
    rows = await _audit(mk, card_id, "card.cancelled")
    assert [r.details["trigger"] for r in rows] == ["vendor.cancel_cards_retry"]

    # Idempotent: a second retry finds nothing live and calls nobody.
    again = AsyncMock(return_value=True)
    with patch(_MOCK_CANCEL, again):
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/vendors/{vid}/cancel-cards")
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled"] == 0
    again.assert_not_awaited()


async def test_cards_switched_off_is_reported_not_closed(realdb, mk):
    """Cards disabled in org settings: we cannot reach the issuer, so the card
    is reported live — never assumed closed."""
    org_id = realdb.info(TENANT).org_id
    vid = await _seed_vendor(mk, org_id)
    card_id = await _seed_card(mk, org_id, vid)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/{vid}/reject")
    assert resp.status_code == 200, resp.text
    rev = resp.json()["card_revocation"]
    assert rev["not_closed"][0]["outcome"] == "cards_not_configured"
    assert await _card_status(mk, card_id) == "created"


async def test_cancel_cards_refuses_a_payable_vendor(realdb, mk, cards_enabled):
    org_id = cards_enabled
    vid = await _seed_vendor(mk, org_id)
    card_id = await _seed_card(mk, org_id, vid)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/{vid}/cancel-cards")
    assert resp.status_code == 409, resp.text
    assert await _card_status(mk, card_id) == "created"


async def test_cancel_cards_requires_a_vendor_duty(realdb, mk, cards_enabled):
    org_id = cards_enabled
    vid = await _seed_vendor(mk, org_id, status="inactive")
    await _seed_card(mk, org_id, vid)

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/vendors/{vid}/cancel-cards")
    assert resp.status_code == 403, resp.text
