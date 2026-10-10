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

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

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


@pytest.mark.asyncio
async def test_the_card_is_minted_net_of_credits_and_takes_no_discount(realdb):
    """Net of applied credit memos — a card is spendable up to its limit, so the
    gross would overpay the credit — but NOT of an accepted early-payment
    discount: a card minted outside a run is no booked payment, so nothing
    would record or capture the discount, and it would outlive the deadline."""
    from datetime import timedelta

    from app.models.credit_memo import CreditMemo
    from app.models.discount import DiscountOffer
    from app.utils.dates import utc_today

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    inv_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="CARDNET"
    )
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        s.add(
            CreditMemo(
                memo_number="CM-CARDNET",
                vendor_id=vendor_id,
                invoice_id=inv_id,
                amount=Decimal("50.00"),
                currency="USD",
                status="applied",
                organization_id=org_id,
            )
        )
        s.add(
            DiscountOffer(
                organization_id=org_id,
                entity_id=inv.entity_id,
                scope="invoice",
                invoice_id=inv_id,
                vendor_id=vendor_id,
                base_amount=Decimal("250.00"),
                currency="USD",
                tiers=[{"days": 10, "percent": "2.00"}],
                status="accepted",
                accepted_tier={"days": 10, "percent": "2.00"},
                valid_from=utc_today() - timedelta(days=1),
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(inv_id)]})
    assert resp.status_code in (200, 201), resp.text

    async with mk() as s:
        card = (
            await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == inv_id))
        ).scalar_one()
    # 250.00 - 50.00 credit; the accepted 2 % offer is left to a booked payment.
    assert card.amount_limit == Decimal("200.00")


# ---------------------------------------------------------------------------
# Concurrency: the checks and the mint are serialised on the invoice row
# ---------------------------------------------------------------------------
#
# The checks above (payable status, live card, live payment, blocking
# exception, vendor status) used to be read unlocked, then the provider was
# called, then the card inserted. Two concurrent requests both passed them. The
# loser minted a real card at the provider (under a fresh idempotency key once
# the winner's row was visible to `reissue_seq`), and the unique index then
# refused to record it. `card_issuance.lock_invoices_for_mint` locks vendors and
# invoices before any check (docs/decisions.md §265).


def _count_card_mints(monkeypatch, on_call=None) -> list:
    from app.services.card_adapters.mock_adapter import MockCardAdapter

    calls: list = []
    original = MockCardAdapter.create_card

    async def counting(self, payload):
        calls.append(payload.invoice_id)
        if on_call is not None:
            await on_call(len(calls))
        return await original(self, payload)

    monkeypatch.setattr(MockCardAdapter, "create_card", counting)
    return calls


async def _a_backend_waits_on_a_lock(mk, *, timeout_s: float = 10.0) -> bool:
    """Poll `pg_stat_activity` until some backend on this tenant DB is blocked
    on a row lock. This waits on the real signal (the second request has
    reached the lock and is queued behind it), not on a sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_concurrent_generates_mint_one_card(realdb, monkeypatch):
    """The second request waits on the invoice lock while the first is at the
    provider, then sees the first one's card and skips it. The provider is
    called once, one card exists, and the loser answers a clean `total: 0`."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="RACE"
    )
    payload = {"invoice_ids": [str(invoice_id)]}

    async with realdb.client(key=TENANT, role="admin") as c:
        second: dict = {}

        async def start_second_and_wait_for_it_to_queue(n: int) -> None:
            if n != 1:
                return
            second["task"] = asyncio.create_task(c.post("/api/cards/generate", json=payload))
            second["queued"] = await _a_backend_waits_on_a_lock(mk)

        calls = _count_card_mints(monkeypatch, start_second_and_wait_for_it_to_queue)
        first = await c.post("/api/cards/generate", json=payload)
        loser = await second["task"]

    assert first.status_code == 201, first.text
    assert first.json()["total"] == 1
    assert second["queued"], "the second request must queue on the lock, not race past it"
    assert loser.status_code == 201, loser.text
    assert loser.json()["total"] == 0
    assert calls == [str(invoice_id)], "the provider must be asked for exactly one card"
    assert len(await _cards_for(mk, invoice_id)) == 1


@pytest.mark.asyncio
async def test_a_vendor_status_change_waits_for_the_mint(realdb, monkeypatch):
    """The vendor row is held `FOR SHARE` until the card commits, so a
    deactivation (or a block) cannot land between the vendor check and the
    card existing. Whatever it does about the vendor's live cards sees this
    one."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="VLOCK"
    )

    seen: list[str] = []

    async def try_to_deactivate(_n: int) -> None:
        async with mk() as s:
            try:
                await s.execute(
                    text("SELECT id FROM vendors WHERE id = :id FOR NO KEY UPDATE NOWAIT"),
                    {"id": vendor_id},
                )
                await s.execute(
                    text("UPDATE vendors SET status = 'inactive' WHERE id = :id"),
                    {"id": vendor_id},
                )
                await s.commit()
                seen.append("committed")
            except DBAPIError as exc:
                await s.rollback()
                assert "could not obtain lock" in str(exc), exc
                seen.append("lock_held")

    _count_card_mints(monkeypatch, try_to_deactivate)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 1
    assert seen == ["lock_held"]


class _HeldInvoiceLock:
    """Hold `FOR NO KEY UPDATE` on one invoice from its own transaction until
    exit. That is the lock a payment dispatch holds across its processor call
    (`api/payments._lock_payment_invoice`), and a card INSERT's `FOR KEY SHARE`
    does not conflict with it. So without the mint's own lock, nothing here
    waits."""

    def __init__(self, mk, invoice_id):
        self._mk = mk
        self._invoice_id = invoice_id

    async def __aenter__(self):
        self._session = self._mk()
        await self._session.execute(
            text("SELECT id FROM invoices WHERE id = :id FOR NO KEY UPDATE"),
            {"id": self._invoice_id},
        )
        return self

    async def __aexit__(self, *exc):
        await self._session.rollback()
        await self._session.close()


@pytest.mark.asyncio
async def test_a_locked_invoice_is_refused_with_409_before_the_provider(realdb, monkeypatch):
    """Past the bound the request refuses by name without calling any
    provider. The refusal is retry-safe: once the holder is gone, the same
    request mints."""
    from app.config import settings

    monkeypatch.setattr(settings, "payment_invoice_lock_timeout_ms", 200)
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="HELD"
    )
    calls = _count_card_mints(monkeypatch)
    payload = {"invoice_ids": [str(invoice_id)]}

    async with _HeldInvoiceLock(mk, invoice_id):
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post("/api/cards/generate", json=payload)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].startswith("invoice_locked")
    assert calls == []
    assert await _cards_for(mk, invoice_id) == []

    async with realdb.client(key=TENANT, role="admin") as c:
        retry = await c.post("/api/cards/generate", json=payload)
    assert retry.status_code == 201, retry.text
    assert retry.json()["total"] == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_locked_invoice_does_not_cost_its_siblings_their_cards(realdb, monkeypatch):
    """Each invoice is locked, minted and committed on its own, so one held
    invoice is skipped (retry-safe: no provider call for it) while the rest of
    the batch is minted and returned."""
    from app.config import settings

    monkeypatch.setattr(settings, "payment_invoice_lock_timeout_ms", 200)
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    held = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="HELD-A"
    )
    free = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="FREE-B"
    )
    calls = _count_card_mints(monkeypatch)

    async with _HeldInvoiceLock(mk, held):
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(held), str(free)]})
    assert resp.status_code == 201, resp.text
    assert [i["invoice_id"] for i in resp.json()["items"]] == [str(free)]
    assert calls == [str(free)]
    assert await _cards_for(mk, held) == []


async def _seed_payment(mk, info, invoice_id, *, method: str, status: str, session=None):
    """A payment on ``invoice_id`` (with its run). Written in ``session`` when
    given (so a test can commit it from a transaction that holds a lock),
    else in its own committed one."""
    from app.models.payment import Payment, PaymentRun

    async def _write(s):
        run = PaymentRun(
            organization_id=info.org_id,
            status="submitted" if status != "pending" else "draft",
            total_amount=Decimal("250.00"),
            initiated_by=info.users["ap_manager"],
            requires_cfo_approval=False,
        )
        s.add(run)
        await s.flush()
        s.add(
            Payment(
                invoice_id=invoice_id,
                payment_run_id=run.id,
                amount=Decimal("250.00"),
                method=method,
                status=status,
            )
        )

    if session is not None:
        await _write(session)
        return
    async with mk() as s:
        await _write(s)
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invoice_status", "method", "payment_status"),
    [
        # An ACH that settled, with the invoice not yet `paid`.
        (InvoiceStatus.payment_scheduled, "ach", "completed"),
        # A run's card payment not yet executed: the run mints at execute.
        (InvoiceStatus.approved, "virtual_card", "pending"),
    ],
)
async def test_an_invoice_already_being_paid_is_not_minted_a_card(
    realdb, monkeypatch, invoice_status, method, payment_status
):
    """`payment_scheduled` is payable, so an invoice whose ACH had already
    settled (but was not yet `paid`) passed the status filter and got a card on
    top of the wire. A live payment is a claim on the invoice, as a live card
    is, on every rail."""
    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=invoice_status, vendor_id=vendor_id, number=f"PAID-{method}"
    )
    await _seed_payment(mk, info, invoice_id, method=method, status=payment_status)
    calls = _count_card_mints(monkeypatch)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 0
    assert calls == []
    assert await _cards_for(mk, invoice_id) == []


@pytest.mark.asyncio
async def test_a_generate_that_waits_out_a_dispatch_sees_its_payment(realdb, monkeypatch):
    """The two halves together. A dispatch holds the invoice while it pays it;
    the generate queues on that lock; the dispatch commits its payment and
    releases. The generate must then see the payment and mint nothing, rather
    than carding an invoice that was paid while it waited."""
    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="WAITED"
    )
    calls = _count_card_mints(monkeypatch)

    async with realdb.client(key=TENANT, role="admin") as c:
        holder = _HeldInvoiceLock(mk, invoice_id)
        await holder.__aenter__()
        try:
            task = asyncio.create_task(
                c.post("/api/cards/generate", json={"invoice_ids": [str(invoice_id)]})
            )
            assert await _a_backend_waits_on_a_lock(mk), "generate must queue on the lock"
            await _seed_payment(
                mk, info, invoice_id, method="ach", status="submitted", session=holder._session
            )
            await holder._session.commit()
        finally:
            await holder.__aexit__(None, None, None)
        resp = await task

    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 0
    assert calls == []
    assert await _cards_for(mk, invoice_id) == []


@pytest.mark.asyncio
# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_an_invoice_outside_the_selected_entity_is_not_minted_a_card(realdb):
    """Like every other money route, the mint is scoped to `X-Entity-ID`. It
    selected by id alone, so a caller working in one subsidiary could put a
    spendable card on another's invoice."""
    from tests.entity_scope_probe import two_entities

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=org_id)
    vendor_id = await _seed_vendor(mk, org_id)
    invoice_id = await _seed_invoice(
        mk, org_id, status=InvoiceStatus.approved, vendor_id=vendor_id, number="SCOPED"
    )
    payload = {"invoice_ids": [str(invoice_id)]}

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await two_entities(c, slug="cardgen-scope")
        outside = await c.post(
            "/api/cards/generate", json=payload, headers={"X-Entity-ID": other_id}
        )
        assert outside.status_code == 201, outside.text
        assert outside.json()["total"] == 0
        assert await _cards_for(mk, invoice_id) == []

        inside = await c.post(
            "/api/cards/generate", json=payload, headers={"X-Entity-ID": default_id}
        )
    assert inside.status_code == 201, inside.text
    assert inside.json()["total"] == 1
