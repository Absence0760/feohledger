"""RBAC pin for `GET /api/cards/{card_id}/details` (full PAN + CVV reveal).

The endpoint used to declare `require_roles(admin, ap_manager, cfo)` at the
route decorator, then immediately re-check a narrower, CONTRADICTORY role set
inline in the handler body (`{"admin", "ap_manager"}`) — silently excluding
`cfo` even though the decorator says it's allowed. Not access-widening (it
failed closed), but dead/contradictory logic that would mislead the next
edit. Resolved by dropping the inline check and keeping the decorator as the
single source of truth: `cfo` is READ access to sensitive financial data
(consistent with every other endpoint in this file, and the read/write split
elsewhere in the codebase — e.g. `positive_pay._READ_ROLES` includes `cfo`,
`_WRITE_ROLES` doesn't). `ap_clerk` stays refused either way — the frontend
e2e `pan-reveal-pii.spec.ts` already pins that case; this test adds the
role-matrix coverage across all four roles at the API layer.

See https://github.com/Absence0760/feohledger/issues/278
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.organization import Organization
from app.models.virtual_card import VirtualCard
from app.models.workflow import AuditLog

TENANT = "a"


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _use_mock_card_adapter(realdb, *, org_id):
    """Route the org at the deterministic mock card adapter (byok/mock) so
    the reveal resolves without a real Lithic/Nium credential."""
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


async def _seed_card(mk, org_id, *, provider_card_id: str) -> VirtualCard:
    from app.models.invoice import Invoice, InvoiceStatus

    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"CARDDETAILS-{provider_card_id}",
            vendor_name="Card Detail Vendor",
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
            provider_card_id=provider_card_id,
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
@pytest.mark.parametrize("role", ["admin", "ap_manager", "cfo"])
async def test_allowed_roles_reveal_card_details_and_write_audit_row(realdb, role):
    """admin / ap_manager / cfo are the decorator's declared role set — every
    one of them must actually reach the PAN, and each successful reveal must
    write the append-only `card.details_viewed` audit row (last_four only)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _use_mock_card_adapter(realdb, org_id=org_id)

    card = await _seed_card(mk, org_id, provider_card_id=f"mock_details_{role}")

    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.get(f"/api/cards/{card.id}/details")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["card_number"]
    assert body["cvv"]

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "card.details_viewed",
                        AuditLog.entity_id == card.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details == {"last_four": "4242"}


@pytest.mark.asyncio
async def test_ap_clerk_is_forbidden_and_writes_no_audit_row(realdb):
    """`ap_clerk` is not in the decorator's role set — refused, and (unlike
    the previous contradictory-gate bug) this is now the ONLY check, so a
    403 must come from the dependency before the handler body ever runs."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _use_mock_card_adapter(realdb, org_id=org_id)

    card = await _seed_card(mk, org_id, provider_card_id="mock_details_clerk")

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(f"/api/cards/{card.id}/details")
    assert resp.status_code == 403, resp.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "card.details_viewed",
                        AuditLog.entity_id == card.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []
