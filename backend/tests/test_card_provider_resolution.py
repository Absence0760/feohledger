"""A NAMED card provider we have no adapter for is refused, never `mock`.

`decisions.md` §29 removed exactly this fallback from `payment_adapters`,
`erp_adapters` and `fx_adapters`, and §36 from `sanctions_adapters` — the card
dispatcher was the one it missed. `MockCardAdapter` is not an inert stub:

  * `create_card` → `success=True`, a synthetic `mock_card_…` id, `last_four="4242"`
  * `get_card_details` → the fixture PAN `4242424242424242`
  * `cancel_card` → `True`, unconditionally

so a single typo in the admin-entered `settings.cards.provider` (a BYOK tenant
writing `"marqeta"`) made every issuance "succeed": `VirtualCard` rows landed
with `card_provider="mock"`, the payment-run card leg marked each payment
`completed` and each invoice `payment_scheduled`, `POST /api/cards/generate`
reported cards minted, and vendors were emailed single-use reveal links
resolving to a fixture PAN. No money moved and nothing failed.

The five call sites each decide what the refusal MEANS (the §29 per-caller
table), and this file pins all five:

  | call site | on refusal |
  |---|---|
  | `issue_card_for_invoice` (batch + run leg) | `card_provider_not_configured`, |
  |   | no card minted, RETRY_SAFE |
  | `POST /api/cards/generate` | 409 naming the bad value — never "0 minted" |
  | `GET /api/cards/{id}/details` | 409 — never the fixture PAN |
  | `POST /api/cards/{id}/cancel` | 409, row NOT flipped to `cancelled` |
  | `cancel_card_at_provider` (void) | `card_provider_not_configured` — never |
  |   | a cancel we did not obtain |
  | `GET /portal/cards/{token}` (vendor) | PII-free degraded body, `pan=None` |

An *unset* provider never raises — it is a normal state. It resolves through
`get_default_provider`, which applies the `REGION_DEFAULTS` preference only once
a credential for that issuer exists and hands back `mock` otherwise (guard rail
7). That gate is pinned in `tests/test_card_provider_local_first.py`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.organization import Organization
from app.models.virtual_card import VirtualCard
from app.services.card_adapters import UnknownCardProviderError, get_card_adapter
from app.services.card_adapters.lithic import LithicAdapter
from app.services.card_adapters.mock_adapter import MockCardAdapter
from app.services.card_adapters.nium import NiumAdapter
from app.services.card_issuance import cancel_card_at_provider, issue_card_for_invoice
from app.services.payment_runs import RETRY_SAFE, classify_payment_failure

# Fake orgs carry their provider secrets inline; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

TENANT = "a"

# The fixture PAN the mock adapter hands out. It must never reach a caller that
# asked for a real provider.
_FIXTURE_PAN = "4242424242424242"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_named_unknown_provider_raises_instead_of_resolving_mock():
    with pytest.raises(UnknownCardProviderError) as exc:
        get_card_adapter({"provider": "marqeta", "region": "US"})
    assert exc.value.provider == "marqeta"
    # The message names the registered alternatives so an admin can fix it.
    assert "lithic" in str(exc.value)
    assert "nium" in str(exc.value)


def test_unknown_provider_name_is_bounded_in_the_error():
    """The provider column is `String(50)`; an oversized settings value must not
    bloat a log line or an HTTP body."""
    with pytest.raises(UnknownCardProviderError) as exc:
        get_card_adapter({"provider": "z" * 500, "region": "US"})
    assert len(exc.value.provider) == 50


def test_unset_provider_resolves_a_default_instead_of_raising(monkeypatch):
    """Local-first: an org that named no issuer is a normal state, never the
    refusal. With a platform credential present it is the region's preferred
    issuer; without one it is `mock` — see
    `tests/test_card_provider_local_first.py` for that half."""
    from app.config import settings

    monkeypatch.setattr(settings, "lithic_api_key", "live_key")
    monkeypatch.setattr(settings, "nium_client_id", "cid")
    monkeypatch.setattr(settings, "nium_client_secret", "sec")
    assert isinstance(get_card_adapter({"region": "US"}), LithicAdapter)
    assert isinstance(get_card_adapter({"region": "ZA"}), NiumAdapter)
    # An explicitly empty string is "unset", not "named".
    assert isinstance(get_card_adapter({"provider": "", "region": "US"}), LithicAdapter)


def test_explicit_mock_is_still_honoured():
    """`mock` REQUESTED by name is the documented local-dev path and stays
    available — the refusal is only for names we don't recognise at all."""
    assert isinstance(get_card_adapter({"provider": "mock", "region": "US"}), MockCardAdapter)


def test_dispatcher_registers_builtins_without_the_caller_importing_them():
    """The refusal is only trustworthy if every built-in adapter had a chance to
    register. The dispatcher imports them itself rather than trusting each call
    site's `import app.services.card_adapters.lithic  # noqa` preamble."""
    from app.services.card_adapters.dispatcher import list_available_providers

    assert {"lithic", "nium", "mock"}.issubset(set(list_available_providers()))


# ---------------------------------------------------------------------------
# issue_card_for_invoice — the money path
# ---------------------------------------------------------------------------


def _invoice():
    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        vendor_name="Acme Corp",
        amount=Decimal("250.00"),
        currency="USD",
        description="Pro services",
    )


def _db(existing_cards: int = 0):
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=existing_cards)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _app_settings():
    return SimpleNamespace(
        lithic_api_key="x",
        lithic_sandbox=True,
        nium_client_id="x",
        nium_client_secret="x",
        nium_customer_hash_id="x",
        nium_wallet_hash_id="x",
        nium_sandbox=True,
    )


@pytest.mark.asyncio
async def test_issue_card_refuses_an_unregistered_provider():
    """No card is minted and the failure reason names the CONDITION, not the
    admin's raw settings value — every AP user reads `failure_reason` while only
    an admin owns the setting (same split as `fx_provider_unsupported`)."""
    result = await issue_card_for_invoice(
        db=_db(),
        invoice=_invoice(),
        organization_id=uuid.uuid4(),
        org_settings={"cards": {"enabled": True, "program_type": "byok", "provider": "marqeta"}},
        app_settings=_app_settings(),
    )
    assert result.success is False
    assert result.card is None
    assert result.failure_reason == "card_provider_not_configured"
    assert "marqeta" not in (result.failure_reason or "")


def test_the_refusal_reason_is_retry_safe():
    """The refusal lands BEFORE any provider call, so no order exists anywhere
    and `/retry-failed` may re-attempt once the setting is fixed. The `_not_configured`
    suffix is `payment_runs`' existing 'per-adapter pre-flight refusal' bucket."""
    assert (
        classify_payment_failure(
            failure_reason="card_provider_not_configured",
            provider_payment_id=None,
        )
        == RETRY_SAFE
    )


@pytest.mark.asyncio
async def test_cancel_for_void_reports_the_refusal_and_never_claims_a_cancel():
    """`mock.cancel_card` returns True unconditionally. Recording a cancel we
    did not obtain is the dangerous direction — the card stays chargeable at the
    real provider while AP believes it is dead."""
    card = SimpleNamespace(id=uuid.uuid4(), provider_card_id="pcard_123")
    outcome = await cancel_card_at_provider(
        card=card,
        org_settings={"cards": {"enabled": True, "program_type": "byok", "provider": "marqeta"}},
        app_settings=_app_settings(),
    )
    assert outcome == "card_provider_not_configured"


# ---------------------------------------------------------------------------
# Supplier-portal PAN reveal — degrades, never leaks the fixture PAN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_reveal_degrades_when_the_provider_is_unregistered():
    from app.api.portal import reveal_card

    card = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        last_four="4321",
        amount_limit=Decimal("100.00"),
        currency="USD",
        expires_at=None,
        provider_card_id="mock_card_xyz",
    )
    db = MagicMock()
    vendor_result = MagicMock()
    vendor_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=vendor_result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    tenant = SimpleNamespace(id=uuid.uuid4(), settings={"cards": {"enabled": True}})

    def _boom(_config):
        raise UnknownCardProviderError("marqeta")

    with (
        patch(
            "app.services.card_reveal.consume_reveal_token",
            AsyncMock(return_value=(card, None)),
        ),
        patch(
            "app.services.card_issuance._resolve_card_config",
            MagicMock(return_value={"provider": "marqeta", "region": "US"}),
        ),
        patch("app.services.card_adapters.get_card_adapter", MagicMock(side_effect=_boom)),
        patch("app.services.audit_dispatch.dispatch_audit", AsyncMock()),
    ):
        body = await reveal_card(token="tok", db=db, tenant=tenant)

    assert body["pan"] is None
    assert body["cvv"] is None
    assert _FIXTURE_PAN not in str(body)
    assert body["warning"]
    # The claim is still committed — a link that survives a failed reveal is
    # indistinguishable from a twice-revealable link.
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# HTTP surface (real DB)
# ---------------------------------------------------------------------------


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _set_card_provider(realdb, org_id, provider: str):
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        settings["cards"] = {"enabled": True, "program_type": "byok", "provider": provider}
        org.settings = settings
        await s.commit()


async def _clear_cards_settings(realdb, org_id):
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        settings.pop("cards", None)
        org.settings = settings
        await s.commit()


async def _seed_card(mk, org_id, *, marker: str) -> VirtualCard:
    from app.models.invoice import Invoice, InvoiceStatus

    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"CARDPROV-{marker}",
            vendor_name="Card Provider Vendor",
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.flush()
        card = VirtualCard(
            organization_id=org_id,
            entity_id=ent,
            invoice_id=inv.id,
            card_provider="mock",
            provider_card_id=f"pcard_{marker}",
            amount_limit=Decimal("500.00"),
            currency="USD",
            status="active",
            last_four="4242",
        )
        s.add(card)
        await s.commit()
        await s.refresh(card)
        return card


@pytest.mark.asyncio
async def test_details_endpoint_409s_instead_of_returning_the_fixture_pan(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    card = await _seed_card(mk, org_id, marker="details")
    await _set_card_provider(realdb, org_id, "marqeta")
    try:
        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.get(f"/api/cards/{card.id}/details")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "marqeta" in detail
        assert "lithic" in detail
        # The fixture PAN never crosses the boundary.
        assert _FIXTURE_PAN not in resp.text
    finally:
        await _clear_cards_settings(realdb, org_id)


@pytest.mark.asyncio
async def test_cancel_endpoint_409s_and_leaves_the_row_active(realdb):
    """`mock.cancel_card` returns True, so the old fallback flipped the row to
    `cancelled` while the real card stayed live and chargeable."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    card = await _seed_card(mk, org_id, marker="cancel")
    await _set_card_provider(realdb, org_id, "marqeta")
    try:
        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(f"/api/cards/{card.id}/cancel")
        assert resp.status_code == 409, resp.text
        async with mk() as s:
            reread = (
                await s.execute(select(VirtualCard).where(VirtualCard.id == card.id))
            ).scalar_one()
            assert reread.status == "active"
    finally:
        await _clear_cards_settings(realdb, org_id)


@pytest.mark.asyncio
async def test_generate_endpoint_refuses_the_batch_rather_than_minting_nothing(realdb):
    """Per-invoice refusal would have reported `total: 0`, which reads as
    "nothing was eligible" — how the misconfiguration stayed invisible."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    card = await _seed_card(mk, org_id, marker="generate")
    await _set_card_provider(realdb, org_id, "marqeta")
    try:
        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(
                "/api/cards/generate", json={"invoice_ids": [str(card.invoice_id)]}
            )
        assert resp.status_code == 409, resp.text
        assert "marqeta" in resp.json()["detail"]
    finally:
        await _clear_cards_settings(realdb, org_id)
