"""The card webhook now cross-checks the URL's `{provider}` segment against
`VirtualCard.card_provider`, not just `provider_card_id`.

Before this fix, `card_webhook`'s DB lookup matched purely on
`provider_card_id == card_token` — the `{provider}` path segment was used
only to pick the Lithic-vs-Nium field-normalization branch, never as a query
filter. `webhook_signing_secret` is one value per org (not per-provider), so
a correctly-signed event posted to the WRONG provider's URL — but carrying a
token that happens to match a card actually issued by the OTHER provider —
would still mutate that card. The collision risk was assessed as negligible
(both providers mint independently-random opaque tokens), so this was
recorded as a "minor / cheap defense-in-depth" gap rather than a primary
finding by the persona-card-processor review, and is fixed here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.virtual_card import CardRebate, VirtualCard

TENANT = "a"
_SECRET = "cross-provider-test-secret"


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _set_webhook_secret(realdb, *, org_id):
    # The signing secret is sealed, not settings (services/provider_credentials).
    from app.services.provider_credentials import update_secrets

    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        await update_secrets(s, org_id, "cards", {"webhook_signing_secret": _SECRET}, [])
        await s.commit()


async def _seed_charged_card(mk, org_id, *, provider: str, token: str) -> uuid.UUID:
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"XPRV-{uuid.uuid4().hex[:8]}",
            vendor_name="Cross Provider Vendor",
            amount=Decimal("250.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.flush()
        card = VirtualCard(
            invoice_id=inv.id,
            organization_id=org_id,
            entity_id=ent,
            card_provider=provider,
            provider_card_id=token,
            amount_limit=Decimal("250.00"),
            currency="USD",
            status="charged",
        )
        s.add(card)
        await s.commit()
        return card.id


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_settlement_posted_to_wrong_provider_path_does_not_settle_the_card(realdb):
    """A card issued by `lithic` must not be found (and thus not mutated) by
    an otherwise-valid, correctly-signed event posted to the `nium` URL, even
    though it carries the SAME token value."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(mk, org_id, provider="lithic", token=token)
    await _set_webhook_secret(realdb, org_id=org_id)

    # Shaped as a Nium settlement event, but referencing the LITHIC card's token.
    body = json.dumps(
        {
            "eventType": "transaction.settled",
            "webhookId": str(uuid.uuid4()),
            "cardHashId": token,
            "amount": "250.00",
            "merchantName": "Attacker Merchant",
        }
    ).encode("utf-8")

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            "/api/cards/webhook/nium",
            content=body,
            headers={"Content-Type": "application/json", "Webhook-Signature": _sign(body)},
        )
    assert resp.status_code == 204

    async with mk() as s:
        card = (await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))).scalar_one()
    assert card.status == "charged", "cross-provider event must not settle a card it doesn't own"
    assert card.merchant_name is None


@pytest.mark.asyncio
async def test_settlement_posted_to_correct_provider_path_still_settles(realdb):
    """Control: the same signed event, posted to the card's REAL provider
    path, still settles it — the fix filters by provider, it doesn't break
    the legitimate path."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(mk, org_id, provider="nium", token=token)
    await _set_webhook_secret(realdb, org_id=org_id)

    body = json.dumps(
        {
            "eventType": "transaction.settled",
            "webhookId": str(uuid.uuid4()),
            "cardHashId": token,
            "amount": "250.00",
            "merchantName": "Real Merchant",
        }
    ).encode("utf-8")

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            "/api/cards/webhook/nium",
            content=body,
            headers={"Content-Type": "application/json", "Webhook-Signature": _sign(body)},
        )
    assert resp.status_code == 204

    async with mk() as s:
        card = (await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))).scalar_one()
        rebate = (
            await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id))
        ).scalar_one_or_none()
    assert card.status == "completed"
    assert rebate is not None, "settlement must have minted the rebate"
