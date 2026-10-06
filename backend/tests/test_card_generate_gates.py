"""HTTP-boundary tests for `POST /api/cards/generate`.

Card issuance has two entry points: this explicit endpoint, and the
`virtual_card` leg of `execute_payment_run` in `api/payments.py`. Both move
real money, so both must enforce the same gates. This endpoint used to
reimplement the mint logic inline and skip every gate the payment-run path
enforces — no `PAYABLE_INVOICE_STATUSES` filter, no
`check_payment_compliance` sanctions/KYC screen, and no audit row. The fix
routes it through the same `issue_card_for_invoice` helper (+ the compliance
gate + an audit dispatch) the payment-run executor already uses.

These tests pin the three closed gaps:
  - an invoice that hasn't cleared AP approval is never minted a card
  - a sanctioned/blocked vendor is never minted a card
  - a successful mint writes an append-only `card.generated` audit row
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.vendor import Vendor
from app.models.virtual_card import VirtualCard
from app.models.workflow import AuditLog

TENANT = "a"


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _enable_cards(realdb, *, org_id):
    """Flip `cards.enabled` on and route the org at the deterministic mock
    card adapter (byok/mock — no real Lithic/Nium credentials needed)."""
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        settings["cards"] = {
            "enabled": True,
            "program_type": "byok",
            "provider": "mock",
        }
        org.settings = settings
        await s.commit()


async def _seed_invoice(mk, org_id, *, status: InvoiceStatus, vendor_id=None, number="CARDGEN"):
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"{number}-{status.value}",
            vendor_name="Card Vendor",
            vendor_id=vendor_id,
            amount=Decimal("250.00"),
            currency="USD",
            status=status,
        )
        s.add(inv)
        await s.commit()
        return inv.id


async def _seed_vendor(mk, org_id, *, name="Card Vendor Co"):
    async with mk() as s:
        ent = await _default_entity_id(s)
        v = Vendor(organization_id=org_id, entity_id=ent, name=name, status="active")
        s.add(v)
        await s.commit()
        return v.id


@pytest.mark.asyncio
async def test_non_payable_invoice_is_not_minted_a_card(realdb):
    """An invoice still in `ready_for_review` (not yet approved) must never
    get a card minted, even if the caller asks for it in the batch. The
    batch doesn't error — it just silently excludes the ineligible invoice
    (matching the existing skip-on-failure / skip-if-already-carded
    behavior of this endpoint), but the invoice MUST NOT end up with a
    VirtualCard row."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)

    vendor_id = await _seed_vendor(mk, org_id)
    payable_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="PAYABLE"
    )
    not_payable_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.ready_for_review, vendor_id=vendor_id, number="NOTPAY"
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/cards/generate",
            json={"invoice_ids": [str(payable_id), str(not_payable_id)]},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Only the approved invoice got a card.
    assert body["total"] == 1
    assert body["items"][0]["invoice_id"] == str(payable_id)

    async with mk() as s:
        carded_invoice_ids = (await s.execute(select(VirtualCard.invoice_id))).scalars().all()
    assert payable_id in carded_invoice_ids
    assert not_payable_id not in carded_invoice_ids


@pytest.mark.asyncio
async def test_blocked_vendor_is_not_minted_a_card(realdb):
    """A vendor with `payments_blocked=True` (a prior sanctions match, or a
    manual AP block) must never receive a virtual card — issuing one moves
    money exactly like an ACH/wire. The compliance gate refuses it before
    the adapter is ever called."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)

    async with mk() as s:
        ent = await _default_entity_id(s)
        vendor = Vendor(
            organization_id=org_id,
            entity_id=ent,
            name="Some Vendor",
            status="active",
            payments_blocked=True,
            payments_blocked_reason="sanctions match",
        )
        s.add(vendor)
        await s.commit()
        vendor_id = vendor.id

    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="BLOCKED"
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []

    async with mk() as s:
        cards = (
            (await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == invoice_id)))
            .scalars()
            .all()
        )
    assert cards == []


@pytest.mark.asyncio
async def test_sanctioned_vendor_name_is_not_minted_a_card(realdb):
    """Belt-and-suspenders on the sanctions screen itself (not just the
    sticky `payments_blocked` flag): a vendor whose NAME matches the
    sanctions provider's blocklist is refused on this, the first-ever
    screen — mirrors `execute_payment_run`'s virtual_card leg calling
    `check_payment_compliance` before minting."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)

    # "blocked party llc" is a fixture name in the mock sanctions adapter's
    # built-in blocklist (services/sanctions_adapters/mock_adapter.py).
    vendor_id = await _seed_vendor(mk, org_id, name="Blocked Party LLC")
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="SANCTIONED"
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 0

    async with mk() as s:
        cards = (
            (await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == invoice_id)))
            .scalars()
            .all()
        )
    assert cards == []


@pytest.mark.asyncio
async def test_successful_mint_writes_audit_row(realdb):
    """A successful direct mint must leave an append-only audit trail, like
    every other card-lifecycle event in this module (cancel, PAN reveal,
    webhook charge/settle)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)

    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="AUDITED"
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 1
    card_id = body["items"][0]["id"]

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "card.generated",
                        AuditLog.organization_id == org_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "virtual_card"
    assert str(row.entity_id) == card_id
    assert row.details["invoice_id"] == str(invoice_id)
    # Money serialises as an exact string-Decimal, never a float.
    assert row.details["amount_limit"] == "250.00"
    assert isinstance(row.details["amount_limit"], str)


@pytest.mark.asyncio
async def test_platform_mode_honors_an_explicit_provider_override(realdb):
    """`program_type: "platform"` (the seeded default for every fresh clone)
    used to always auto-select lithic/nium by region, discarding any
    admin-set `provider` override entirely — so a platform-mode org could
    never point local-first issuance at `mock`, and instead silently made a
    live outbound call to the real sandbox host with no credential
    configured. This end-to-end reproduces the persona's exact live repro:
    `program_type: "platform"` + an explicit `provider: "mock"` override
    must actually mint a mock card, not no-op or reach the network."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        settings["cards"] = {
            "enabled": True,
            "program_type": "platform",
            "region": "US",  # would otherwise resolve to lithic
            "provider": "mock",
        }
        org.settings = settings
        await s.commit()

    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="PLATOVERRIDE"
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 1, body
    assert body["items"][0]["card_provider"] == "mock"


@pytest.mark.asyncio
async def test_minted_card_follows_the_invoice_entity(realdb):
    """The card follows the invoice it pays (multi-entity P2) — a card minted
    for an invoice under a non-default `Entity` must carry that same
    `entity_id`, not the tenant's default entity. `issue_card_for_invoice`
    (shared with `execute_payment_run`'s virtual_card leg) previously dropped
    `entity_id` entirely; the mint would have been invisible to `GET
    /api/cards` and the dashboard under an `X-Entity-ID`-scoped read."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)

    from app.models.entity import Entity

    async with mk() as s:
        sub = Entity(organization_id=org_id, name="EU Subsidiary", slug="eu-sub", is_default=False)
        s.add(sub)
        await s.commit()
        sub_id = sub.id

    vendor_id = await _seed_vendor(mk, org_id)
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=sub_id,
            invoice_number="ENTITYCARD-1",
            vendor_name="Card Vendor",
            vendor_id=vendor_id,
            amount=Decimal("250.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        invoice_id = inv.id

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 1

    async with mk() as s:
        card = (
            await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == invoice_id))
        ).scalar_one()
    assert card.entity_id == sub_id


async def _cards_for(mk, invoice_id):
    async with mk() as s:
        return (
            (await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == invoice_id)))
            .scalars()
            .all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor_status", ["unverified", "inactive", "rejected"])
async def test_a_vendor_that_is_not_active_is_not_minted_a_card(realdb, vendor_status):
    """A minted card is spendable the moment it exists, so it is a payment.
    A run refuses an invoice whose vendor is not verified and active; this
    entry point must not mint around that refusal."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)

    async with mk() as s:
        ent = await _default_entity_id(s)
        vendor = Vendor(
            organization_id=org_id,
            entity_id=ent,
            name=f"Vendor {vendor_status}",
            status=vendor_status,
        )
        s.add(vendor)
        await s.commit()
        vendor_id = vendor.id

    invoice_id = await _seed_invoice(
        mk,
        org_id,
        status=InvoiceStatus.approved,
        vendor_id=vendor_id,
        number=f"VND-{vendor_status}",
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 0
    assert await _cards_for(mk, invoice_id) == []


@pytest.mark.asyncio
async def test_an_invoice_with_a_payment_blocking_exception_is_not_minted_a_card(realdb):
    """Same gate a run applies: an unresolved payment-blocking exception (here
    a failed quality inspection) stops the mint, while a clean invoice in the
    same batch is still carded."""
    from app.models.exception import Exception as APException

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id, name="Card Vendor Blocked Exc")
    held = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="QHOLD"
    )
    clean = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="QCLEAN"
    )
    async with mk() as s:
        s.add(
            APException(
                organization_id=org_id,
                invoice_id=held,
                exception_type="quality_hold",
                severity="error",
                description="seeded by test",
                status="open",
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(held), str(clean)]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 1
    assert await _cards_for(mk, held) == []
    assert len(await _cards_for(mk, clean)) == 1
