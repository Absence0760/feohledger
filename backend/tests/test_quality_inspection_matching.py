"""4-way matching (quality inspection leg) — service + warnings integration.

Inserts a PO + GR + QualityInspection via the realdb tenant session maker, runs
``match_invoice_to_po``, and asserts the 4-way outcomes for pass / fail /
partial plus the require_inspection-missing path. Also drives
``invoice_warnings._refresh_po_match`` end-to-end to prove a failed inspection
raises a ``quality_hold`` exception. Money/quantities are ``Decimal``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest


def _messages(result) -> list[str]:
    """The English fallback of each `po_match.issue.*` finding."""
    return [i["message"] for i in result.issues]


async def _default_entity_id(session) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.entity import Entity

    return (await session.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed_po_gr(session, org_id, entity_id, *, with_lines=False):
    """Create a PO (+optional line) and a GR (+optional line) for it.

    Returns (po, gr).
    """
    from app.models.procurement import (
        GoodsReceipt,
        GRLineItem,
        POLineItem,
        PurchaseOrder,
    )

    po = PurchaseOrder(
        po_number="PO-INSP-1",
        total=Decimal("100.00"),
        status="open",
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(po)
    await session.flush()

    gr = GoodsReceipt(
        gr_number="GR-INSP-1",
        po_id=po.id,
        status="received",
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(gr)
    await session.flush()

    if with_lines:
        session.add(POLineItem(po_id=po.id, description="Widget", quantity=Decimal("10.0000")))
        session.add(
            GRLineItem(gr_id=gr.id, description="Widget", quantity_received=Decimal("10.0000"))
        )
        await session.flush()

    return po, gr


async def _make_invoice(session, org_id, entity_id, *, amount="100.00"):
    from app.models.invoice import Invoice

    inv = Invoice(
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        vendor_name="Acme",
        amount=Decimal(amount),
        po_number="PO-INSP-1",
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(inv)
    await session.flush()
    return inv


async def _add_inspection(session, org_id, entity_id, gr_id, po_id, **kw):
    from app.models.quality_inspection import QualityInspection

    qi = QualityInspection(
        inspection_number=kw.pop("inspection_number", "QI-1"),
        gr_id=gr_id,
        po_id=po_id,
        organization_id=org_id,
        entity_id=entity_id,
        **kw,
    )
    session.add(qi)
    await session.flush()
    return qi


@pytest.mark.parametrize("header_role", ["admin"])
async def test_match_pass_is_4way(realdb, header_role):
    from app.services.po_matching import match_invoice_to_po

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, gr = await _seed_po_gr(s, org_id, ent)
        await _add_inspection(s, org_id, ent, gr.id, po.id, result="pass")
        inv = await _make_invoice(s, org_id, ent)
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "4-way"
    assert match.inspection_result == "pass"
    assert match.inspection_id is not None
    # A passing inspection leaves the (within-tolerance) match status intact.
    assert match.status == "matched"
    assert match.details.get("has_inspection") is True


async def test_match_fail_is_mismatch(realdb):
    from app.services.po_matching import match_invoice_to_po

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, gr = await _seed_po_gr(s, org_id, ent)
        await _add_inspection(
            s,
            org_id,
            ent,
            gr.id,
            po.id,
            result="fail",
            deviation_notes="Cracked casings",
        )
        inv = await _make_invoice(s, org_id, ent)
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "4-way"
    assert match.inspection_result == "fail"
    assert match.status == "mismatch"
    assert any("quality inspection" in i.lower() for i in _messages(match))
    # Deviation notes surface in the issue text.
    assert any("Cracked casings" in i for i in _messages(match))


async def test_match_partial_acceptance(realdb):
    from app.services.po_matching import match_invoice_to_po

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, gr = await _seed_po_gr(s, org_id, ent)
        await _add_inspection(
            s,
            org_id,
            ent,
            gr.id,
            po.id,
            result="partial",
            accepted_quantity=Decimal("7.0000"),
        )
        inv = await _make_invoice(s, org_id, ent)
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "4-way"
    assert match.inspection_result == "partial"
    assert match.status == "partial"
    assert match.inspection_accepted_quantity == 7.0
    assert any("partial acceptance" in i.lower() for i in _messages(match))


async def test_require_inspection_missing(realdb):
    from app.services.po_matching import match_invoice_to_po

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _seed_po_gr(s, org_id, ent)
        inv = await _make_invoice(s, org_id, ent)
        await s.commit()

        # No inspection rows exist; require_inspection surfaces it as required.
        match = await match_invoice_to_po(s, inv, require_inspection=True)

    assert match.inspection_required is True
    assert match.inspection_result is None
    assert any("quality inspection required" in i.lower() for i in _messages(match))


async def test_refresh_po_match_raises_quality_hold_on_fail(realdb):
    from sqlalchemy import select

    from app.models.exception import Exception as APException
    from app.services.invoice_warnings import _refresh_po_match

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, gr = await _seed_po_gr(s, org_id, ent)
        await _add_inspection(
            s, org_id, ent, gr.id, po.id, result="fail", deviation_notes="Bad batch"
        )
        inv = await _make_invoice(s, org_id, ent)
        await s.commit()

        warnings: list[dict] = []
        await _refresh_po_match(s, inv, warnings, org_settings={})
        await s.commit()

        exc_rows = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == inv.id,
                        APException.exception_type == "quality_hold",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert any(w["type"] == "quality_hold" for w in warnings)
    assert len(exc_rows) == 1
    assert exc_rows[0].severity == "error"
