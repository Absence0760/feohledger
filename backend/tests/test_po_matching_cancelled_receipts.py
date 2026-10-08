"""The 3-way leg must not count a CANCELLED goods receipt as delivered goods.

`po_matching.match_invoice_to_po` summed `quantity_received` across every
`GoodsReceipt` for the PO with no reference to `GoodsReceipt.status`, so a
delivery the business had explicitly cancelled or reversed still filled the
receipt leg. Two consequences, both silent:

  * the invoice read `matched` (a full 3-way match) when nothing had actually
    been received — the exact thing the 3-way control exists to prevent;
  * a short receipt whose shortfall was covered by a cancelled GR never
    downgraded to `partial`, so no `po_mismatch` info exception was raised.

The mirror-image gap was the long side: over-receipt. Only `received <
ordered` was ever flagged, so MORE units booked in than were ordered passed in
silence — and an over-delivery is how an invoice for quantities nobody
authorised acquires its supporting receipt. It now sets the additive
`over_receipt` flag and appends an issue (the invoice modal renders `issues`
verbatim); `status` is deliberately left to the amount control, which is the
gate that decides whether the invoice is payable.

Runs against the opt-in `realdb` fixture, so the exclusion is proven in the
matcher's real SQL rather than against a mocked result set.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import GoodsReceipt, GRLineItem, POLineItem, PurchaseOrder
from app.services.po_matching import match_invoice_to_po


def _messages(result) -> list[str]:
    """The English fallback of each `po_match.issue.*` finding."""
    return [i["message"] for i in result.issues]


TENANT = "a"


async def _default_entity_id(session):
    return (await session.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _add_po(session, org_id, entity_id, *, po_number, total, lines=()):
    po = PurchaseOrder(
        po_number=po_number,
        total=Decimal(total),
        status="open",
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(po)
    await session.flush()
    for qty in lines:
        session.add(POLineItem(po_id=po.id, description="Widget", quantity=Decimal(qty)))
    await session.flush()
    return po


async def _add_gr(session, org_id, entity_id, po_id, *, status="received", received=()):
    gr = GoodsReceipt(
        gr_number=f"GR-{uuid.uuid4().hex[:8]}",
        po_id=po_id,
        status=status,
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(gr)
    await session.flush()
    for qty in received:
        session.add(GRLineItem(gr_id=gr.id, description="Widget", quantity_received=Decimal(qty)))
    await session.flush()
    return gr


async def _add_invoice(session, org_id, entity_id, *, po_number, amount):
    inv = Invoice(
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        vendor_name="Acme",
        amount=Decimal(amount),
        po_number=po_number,
        # `refresh_warnings` deliberately skips PO matching on a draft `new`
        # invoice (nothing has been extracted yet), so the end-to-end tests
        # need a post-extraction status.
        status=InvoiceStatus.ready_for_review,
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(inv)
    await session.flush()
    return inv


@pytest.mark.asyncio
async def test_cancelled_receipt_does_not_satisfy_the_quantity_match(realdb):
    """A cancelled GR covering the full order must not read as a 3-way match.

    Pre-fix the cancelled receipt's 10 units were summed like any other, so the
    result was `3-way` / `matched` — an invoice cleared against goods the
    business had recorded as never received.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CANC-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="cancelled", received=["10"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1000.00")
        await s.commit()

        result = await match_invoice_to_po(s, inv)

    # Every receipt was cancelled: the business recorded that nothing arrived.
    # That is a 3-way match with zero received — partial, and billed beyond
    # receipt — not a 2-way fallback that would read the full bill as clean
    # (decisions §262 amends the earlier "no evidence → 2-way" reading).
    assert result.match_type == "3-way", result.issues
    assert result.gr_id is None
    assert result.details["has_gr"] is False
    assert result.details["all_receipts_cancelled"] is True
    assert result.received_quantity == 0
    assert result.status == "partial"
    assert result.billed_beyond_receipt is True


@pytest.mark.asyncio
async def test_cancelled_receipt_does_not_top_up_a_partial_delivery(realdb):
    """A live 4-of-10 receipt plus a cancelled 6 is still a PARTIAL receipt.

    This is the shape that hid the bug in a tenant with real traffic: the
    shortfall looks covered, so the invoice reads `matched` and no
    `po_mismatch` info exception is raised on the 60% that never arrived.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CANC-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="received", received=["4"])
        await _add_gr(s, org_id, ent, po.id, status="cancelled", received=["6"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1000.00")
        await s.commit()

        result = await match_invoice_to_po(s, inv)

    assert result.match_type == "3-way"
    assert result.status == "partial", result.issues
    assert any("Partial receipt: 40%" in i for i in _messages(result)), result.issues
    assert result.over_receipt is False


@pytest.mark.asyncio
async def test_live_receipts_still_aggregate_across_shipments(realdb):
    """The guard is an exclusion list, not an allowlist — live statuses count.

    Two live shipments of 6 + 4 against a 10-unit order stay a full 3-way
    match, including a status the roster has never heard of.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-LIVE-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="received", received=["6"])
        await _add_gr(s, org_id, ent, po.id, status="partially_received", received=["4"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1000.00")
        await s.commit()

        result = await match_invoice_to_po(s, inv)

    assert result.match_type == "3-way"
    assert result.status == "matched", result.issues
    assert result.issues == []


@pytest.mark.asyncio
async def test_over_receipt_is_flagged(realdb):
    """More received than ordered raises the over-receipt flag + issue.

    Pre-fix only `received < ordered` was ever reported, so 14 units booked
    against a 10-unit order produced an empty `issues` list and a clean
    `matched` — the receiving discrepancy never reached the reviewer.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-OVER-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="received", received=["14"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1000.00")
        await s.commit()

        result = await match_invoice_to_po(s, inv)

    assert result.over_receipt is True
    assert result.details["over_receipt"] is True
    # Quantities render without the Numeric(12, 4) trailing zeros.
    assert any(
        i == "Over-receipt: 14 received against 10 ordered (+4)" for i in _messages(result)
    ), result.issues
    # The amount control still owns `status` — an in-tolerance amount stays
    # `matched`; over-receipt is a receiving discrepancy, not a billing one.
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_over_receipt_survives_the_json_boundary(realdb):
    """`over_receipt` is additive on the persisted `invoice.po_match` shape.

    `_refresh_warnings` stores `to_json_dict()` on a JSONB column the invoice
    modal reads, so the new field has to serialise alongside the existing keys
    rather than replacing any of them.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-JSON-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="received", received=["14"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1000.00")
        await s.commit()

        payload = (await match_invoice_to_po(s, inv)).to_json_dict()

    assert payload["over_receipt"] is True
    # The pre-existing keys every downstream reader relies on are untouched.
    for key in ("status", "match_type", "po_number", "po_total", "issues", "details"):
        assert key in payload


async def _po_mismatch_rows(s, invoice_id):
    return (
        (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == "po_mismatch",
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_over_receipt_billed_inside_tolerance_is_a_warning_not_a_hold(realdb):
    """The whole chain: real rows -> matcher -> refresh_warnings -> invoice.

    The invoice bills 1,040 against a 1,000 PO — inside the 5 % amount
    tolerance — while 14 units arrived against 10 ordered. `po_mismatch` blocks
    payment, and decisions §67 is explicit that an over-receipt with an
    in-tolerance amount is a receiving discrepancy that must not: absorbing a
    variance this size is what the tolerance is FOR. So the finding lands on the
    invoice, where the approver reads it, and opens no exception.
    """
    from app.services.invoice_warnings import refresh_warnings

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-E2E-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="received", received=["14"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1040.00")
        await s.commit()

        await refresh_warnings(s, inv)
        await s.commit()

        assert inv.po_match["over_receipt"] is True
        assert inv.po_match["within_tolerance"] is True
        assert any(
            w["type"] == "po_mismatch" and "Over-receipt" in w["message"]
            for w in (inv.warnings or [])
        ), inv.warnings
        assert await _po_mismatch_rows(s, inv.id) == []


@pytest.mark.asyncio
async def test_over_receipt_billed_beyond_tolerance_holds_on_the_amount_leg(realdb):
    """When the over-delivered units ARE billed beyond tolerance (1,400 against
    a 1,000 PO), the amount leg opens the blocking row on its own figure — the
    over-receipt never needed a route of its own to stop the money."""
    from app.services.invoice_warnings import refresh_warnings

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-E2EO-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="received", received=["14"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1400.00")
        await s.commit()

        await refresh_warnings(s, inv)
        await s.commit()

        rows = await _po_mismatch_rows(s, inv.id)
        assert len(rows) == 1, rows
        # `_add_po` records no currency, so it is the face-value variant.
        assert rows[0].description_code.startswith("po_amount_variance")
        assert rows[0].status == "open"


@pytest.mark.asyncio
async def test_over_receipt_on_an_invoice_billing_the_po_total_is_a_warning_only(realdb):
    """An over-delivery on an invoice that bills exactly what was ordered pays
    exactly what was ordered — a receiving-side discrepancy (decisions §67), not
    a billing one. `po_mismatch` blocks payment, so raising one here would hold
    a correctly-billed payable; the finding stays a warning on the invoice,
    where the approver still reads it."""
    from app.services.invoice_warnings import refresh_warnings

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-E2EW-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="received", received=["14"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1000.00")
        await s.commit()

        await refresh_warnings(s, inv)
        await s.commit()

        assert inv.po_match["over_receipt"] is True
        assert any(
            w["type"] == "po_mismatch" and "Over-receipt" in w["message"]
            for w in (inv.warnings or [])
        ), inv.warnings
        assert await _po_mismatch_rows(s, inv.id) == []


@pytest.mark.asyncio
async def test_cancelled_receipt_does_not_open_an_over_receipt_exception(realdb):
    """A cancelled over-delivery raises no over-receipt — but it is not nothing.

    A cancelled GR of 14 against a 10-unit order would have tripped the
    over-receipt branch if the status filter were ever dropped, so this pins
    that it does not. What it DOES leave is zero received against a full bill,
    which holds the invoice as billed beyond receipt (decisions §262).
    """
    from app.services.invoice_warnings import refresh_warnings

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-E2EC-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", lines=["10"])
        await _add_gr(s, org_id, ent, po.id, status="cancelled", received=["14"])
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1000.00")
        await s.commit()

        await refresh_warnings(s, inv)
        await s.commit()

        assert inv.po_match["over_receipt"] is False
        assert inv.po_match["match_type"] == "3-way"
        assert inv.po_match["billed_beyond_receipt"] is True
        rows = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == inv.id,
                        APException.exception_type == "po_mismatch",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.description_code for r in rows] == ["po_partial_receipt"]


def test_cancelled_status_roster_is_lowercase_and_covers_both_spellings():
    """The column is free-form text, so the roster is matched case-folded.

    A guard on the roster itself: the SQL compares `lower(status)`, so an entry
    added in mixed case would never match and the exclusion would quietly stop
    working for that status.
    """
    from app.services.po_matching import CANCELLED_GR_STATUSES

    assert CANCELLED_GR_STATUSES == {s.lower() for s in CANCELLED_GR_STATUSES}
    assert {"cancelled", "canceled"} <= CANCELLED_GR_STATUSES
