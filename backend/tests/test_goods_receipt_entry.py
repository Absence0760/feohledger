"""Recording and cancelling a goods receipt — `POST /api/goods-receipts`,
`POST /api/goods-receipts/{id}/cancel`, and what they do to the 3-way match.

Real-Postgres harness (`realdb`). Before this endpoint nothing in the app could
write a receipt, so the 3-way leg and the "billed beyond receipt" payment hold
(decisions §249) had no real input. Pinned here:

  * the write — line-by-line against the PO, description from the PO line,
    entity from the PO, recorder stamped, audited;
  * every refusal — cancelled PO, a line not on the PO, a line twice, nothing
    received, a free-text line with no description, a future date, a taken
    number, a reused idempotency key, out-of-scope / cross-tenant PO, roles;
  * idempotency — a replayed key returns the same receipt with 200;
  * the matching consequence — a receipt recorded by someone else lifts the
    hold in the same request; one recorded by the invoice's own uploader does
    not (decisions §261); cancelling a receipt raises it again;
  * the PO detail's per-line received quantities, which the form reads.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import GoodsReceipt, GRLineItem, POLineItem, PurchaseOrder
from app.models.workflow import AuditLog
from app.services.payment_runs import blocked_invoice_ids
from app.utils.dates import utc_today

TENANT = "a"


async def _default_entity(s):
    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _po(
    realdb,
    *,
    lines=(("Wilson Pro Staff racket", "10"),),
    status="open",
    total="1037.00",
    key=TENANT,
    entity_id=None,
):
    """A PO with `lines` of (description, quantity). Returns (po_id, po_number, [line ids])."""
    mk = realdb.sessionmaker(key)
    number = f"PO-GR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = entity_id or await _default_entity(s)
        po = PurchaseOrder(
            po_number=number,
            total=Decimal(total),
            currency="USD",
            status=status,
            organization_id=realdb.info(key).org_id,
            entity_id=ent,
        )
        s.add(po)
        await s.flush()
        line_ids = []
        for desc, qty in lines:
            li = POLineItem(po_id=po.id, description=desc, quantity=Decimal(qty))
            s.add(li)
            await s.flush()
            line_ids.append(li.id)
        await s.commit()
        return po.id, number, line_ids


async def _invoice(realdb, po_number, *, amount="1037.00", uploaded_by=None):
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        inv = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Racket Wholesale Co",
            amount=Decimal(amount),
            currency="USD",
            po_number=po_number,
            status=InvoiceStatus.ready_for_review,
            uploaded_by_id=uploaded_by,
            organization_id=realdb.info(TENANT).org_id,
            entity_id=await _default_entity(s),
        )
        s.add(inv)
        await s.commit()
        return inv.id


async def _po_mismatch_rows(realdb, invoice_id):
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
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


async def _blocked(realdb, invoice_id) -> bool:
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        return invoice_id in await blocked_invoice_ids(s, [invoice_id])


def _body(po_id, line_ids, qtys, **extra):
    return {
        "po_id": str(po_id),
        "received_date": utc_today().isoformat(),
        "lines": [
            {"po_line_item_id": str(lid), "quantity_received": q}
            for lid, q in zip(line_ids, qtys, strict=True)
        ],
        **extra,
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_records_a_receipt_line_by_line(realdb):
    po_id, number, (li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(
            "/api/goods-receipts",
            json={
                **_body(po_id, [li], ["9"]),
                # A client-sent description on a linked line is ignored — the
                # receipt names what the PO line names.
                "lines": [
                    {
                        "po_line_item_id": str(li),
                        "quantity_received": "9",
                        "description": "something else",
                    }
                ],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["po_id"] == str(po_id)
    assert body["po_number"] == number
    assert body["gr_number"] == f"GR-{number}-1"
    assert body["status"] == "received"
    assert body["source"] == "manual"
    assert body["received_date"] == utc_today().isoformat()
    (line,) = body["line_items"]
    assert line["po_line_item_id"] == str(li)
    assert line["description"] == "Wilson Pro Staff racket"
    assert line["quantity_received"] == 9.0

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        gr = (
            await s.execute(select(GoodsReceipt).where(GoodsReceipt.id == uuid.UUID(body["id"])))
        ).scalar_one()
        po_entity = (
            await s.execute(select(PurchaseOrder.entity_id).where(PurchaseOrder.id == po_id))
        ).scalar_one()
        assert gr.recorded_by_user_id == realdb.info(TENANT).users["ap_clerk"]
        assert gr.entity_id == po_entity
        stored = (
            await s.execute(select(GRLineItem.quantity_received).where(GRLineItem.gr_id == gr.id))
        ).scalar_one()
        assert stored == Decimal("9.0000")

        (audit,) = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "goods_receipt.created", AuditLog.entity_id == gr.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert audit.actor_id == realdb.info(TENANT).users["ap_clerk"]
        assert audit.details == {
            "gr_number": f"GR-{number}-1",
            "po_number": number,
            "line_count": 1,
            "quantity_received": "9",
            "received_date": utc_today().isoformat(),
        }


@pytest.mark.asyncio
async def test_a_second_delivery_gets_the_next_number_and_the_po_shows_what_is_left(realdb):
    po_id, number, (a, b) = await _po(realdb, lines=(("Rackets", "10"), ("Grips", "20")))
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post("/api/goods-receipts", json=_body(po_id, [a, b], ["6", "0"]))
        second = await c.post("/api/goods-receipts", json=_body(po_id, [a, b], ["3", "20"]))
        assert first.status_code == 201 and second.status_code == 201
        assert second.json()["gr_number"] == f"GR-{number}-2"

        po = (await c.get(f"/api/purchase-orders/{po_id}")).json()
    received = {li["id"]: li["quantity_received"] for li in po["line_items"]}
    assert received == {str(a): 9.0, str(b): 20.0}
    assert po["quantity_received_total"] == 29.0


@pytest.mark.asyncio
async def test_a_po_without_lines_takes_described_lines(realdb):
    po_id, _, _ = await _po(realdb, lines=())
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        missing = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [{"quantity_received": "4"}],
            },
        )
        ok = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [{"description": "Ball machine", "quantity_received": "1"}],
            },
        )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "goods_receipt_line_required"
    assert ok.status_code == 201
    assert ok.json()["line_items"][0]["description"] == "Ball machine"
    assert ok.json()["line_items"][0]["po_line_item_id"] is None


@pytest.mark.asyncio
async def test_a_typed_delivery_note_number_is_kept_and_must_be_free(realdb):
    po_id, _, (li,) = await _po(realdb)
    note = f"DN-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["2"], gr_number=note))
        # Case-folded: `dn-…` is the same delivery note.
        taken = await c.post(
            "/api/goods-receipts", json=_body(po_id, [li], ["2"], gr_number=note.lower())
        )
    assert first.status_code == 201 and first.json()["gr_number"] == note
    assert taken.status_code == 409
    assert taken.json()["detail"]["code"] == "goods_receipt_number_taken"
    assert taken.json()["detail"]["params"] == {"grNumber": note.lower()}


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_line_refusals(realdb):
    po_id, _, (li,) = await _po(realdb)
    other_po, _, (other_li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        foreign = await c.post("/api/goods-receipts", json=_body(po_id, [other_li], ["1"]))
        unlinked = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [{"description": "Rackets", "quantity_received": "1"}],
            },
        )
        twice = await c.post("/api/goods-receipts", json=_body(po_id, [li, li], ["1", "1"]))
        nothing = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["0"]))
        negative = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["-1"]))
        empty = await c.post(
            "/api/goods-receipts",
            json={"po_id": str(po_id), "received_date": utc_today().isoformat(), "lines": []},
        )
    assert foreign.status_code == 422
    assert foreign.json()["detail"]["code"] == "goods_receipt_line_not_on_po"
    assert unlinked.status_code == 422
    assert unlinked.json()["detail"]["code"] == "goods_receipt_line_not_on_po"
    assert twice.status_code == 422
    assert twice.json()["detail"]["code"] == "goods_receipt_line_duplicated"
    assert nothing.status_code == 422
    assert nothing.json()["detail"]["code"] == "goods_receipt_nothing_received"
    assert negative.status_code == 422
    assert empty.status_code == 422

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        assert (
            await s.execute(select(GoodsReceipt).where(GoodsReceipt.po_id.in_([po_id, other_po])))
        ).first() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "Canceled", "VOID"])
async def test_nothing_is_received_against_a_cancelled_po(realdb, status):
    po_id, number, (li,) = await _po(realdb, status=status)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["1"]))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "goods_receipt_po_cancelled"
    assert resp.json()["detail"]["params"] == {"poNumber": number}


@pytest.mark.asyncio
async def test_a_future_received_date_is_refused_beyond_a_day(realdb):
    po_id, _, (li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        tomorrow = await c.post(
            "/api/goods-receipts",
            json={
                **_body(po_id, [li], ["1"]),
                "received_date": (utc_today() + timedelta(days=1)).isoformat(),
            },
        )
        later = await c.post(
            "/api/goods-receipts",
            json={
                **_body(po_id, [li], ["1"]),
                "received_date": (utc_today() + timedelta(days=2)).isoformat(),
            },
        )
    # A user east of UTC is already on tomorrow's date.
    assert tomorrow.status_code == 201
    assert later.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_or_other_tenants_po_is_a_404(realdb):
    _, _, _ = await _po(realdb)
    b_po, _, (b_li,) = await _po(realdb, key="b")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        unknown = await c.post(
            "/api/goods-receipts", json=_body(uuid.uuid4(), [uuid.uuid4()], ["1"])
        )
        cross = await c.post("/api/goods-receipts", json=_body(b_po, [b_li], ["1"]))
    assert unknown.status_code == 404
    assert cross.status_code == 404
    assert cross.json()["detail"] == unknown.json()["detail"] == "Purchase order not found"


@pytest.mark.asyncio
async def test_a_po_in_a_sibling_entity_is_a_404(realdb):
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        sibling = Entity(
            name=f"UK Ltd {uuid.uuid4().hex[:4]}",
            slug=f"uk-{uuid.uuid4().hex[:6]}",
            organization_id=realdb.info(TENANT).org_id,
        )
        s.add(sibling)
        await s.commit()
        default = await _default_entity(s)
    po_id, _, (li,) = await _po(realdb, entity_id=sibling.id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/goods-receipts",
            json=_body(po_id, [li], ["1"]),
            headers={"X-Entity-ID": str(default)},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_roles(realdb):
    po_id, _, (li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        cfo = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["1"]))
    async with realdb.client(key=TENANT, role=None) as c:
        anon = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["1"]))
    assert cfo.status_code == 403
    assert anon.status_code == 401


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_replayed_key_returns_the_same_receipt_once(realdb):
    po_id, _, (li,) = await _po(realdb)
    other_po, _, (other_li,) = await _po(realdb)
    key = str(uuid.uuid4())
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(
            "/api/goods-receipts", json=_body(po_id, [li], ["5"]), headers={"Idempotency-Key": key}
        )
        again = await c.post(
            "/api/goods-receipts", json=_body(po_id, [li], ["5"]), headers={"Idempotency-Key": key}
        )
        elsewhere = await c.post(
            "/api/goods-receipts",
            json=_body(other_po, [other_li], ["5"]),
            headers={"Idempotency-Key": key},
        )
        po = (await c.get(f"/api/purchase-orders/{po_id}")).json()
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert elsewhere.status_code == 409
    assert elsewhere.json()["detail"]["code"] == "goods_receipt_idempotency_reused"
    # Booked once: five received, not ten.
    assert po["quantity_received_total"] == 5.0


# --------------------------------------------------------------------------- #
# Cancel
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancel_stops_the_receipt_counting(realdb):
    po_id, _, (li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        gr = (await c.post("/api/goods-receipts", json=_body(po_id, [li], ["7"]))).json()
        cancelled = await c.post(f"/api/goods-receipts/{gr['id']}/cancel")
        twice = await c.post(f"/api/goods-receipts/{gr['id']}/cancel")
        po = (await c.get(f"/api/purchase-orders/{po_id}")).json()
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert twice.status_code == 409
    assert twice.json()["detail"]["code"] == "goods_receipt_already_cancelled"
    assert po["quantity_received_total"] == 0.0
    assert po["line_items"][0]["quantity_received"] == 0.0

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        (audit,) = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "goods_receipt.cancelled",
                        AuditLog.entity_id == uuid.UUID(gr["id"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert audit.details["old_status"] == "received"
    assert audit.details["new_status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_receipt_from_elsewhere_cannot_be_cancelled_here(realdb):
    po_id, _, _ = await _po(realdb)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        gr = GoodsReceipt(
            gr_number=f"ERP-{uuid.uuid4().hex[:6]}",
            po_id=po_id,
            status="received",
            organization_id=realdb.info(TENANT).org_id,
            entity_id=await _default_entity(s),
        )
        s.add(gr)
        await s.commit()
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/goods-receipts/{gr.id}/cancel")
        missing = await c.post(f"/api/goods-receipts/{uuid.uuid4()}/cancel")
    async with realdb.client(key=TENANT, role="cfo") as c:
        cfo = await c.post(f"/api/goods-receipts/{gr.id}/cancel")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "goods_receipt_not_manual"
    assert missing.status_code == 404
    assert cfo.status_code == 403


# --------------------------------------------------------------------------- #
# The matching consequence (decisions §249, §261)
# --------------------------------------------------------------------------- #


async def _held_invoice(realdb, *, uploaded_by):
    """Full PO billed with six of ten units in, so the invoice bills beyond what
    arrived: a payment-blocking `po_mismatch`. (Not a round figure, so the
    round-amount fraud rule stays out of the blocking set.)"""
    po_id, number, (li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["6"]))
        assert first.status_code == 201
    inv_id = await _invoice(realdb, number, uploaded_by=uploaded_by)
    # The invoice's own refresh raises the hold.
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        from app.services.invoice_warnings import refresh_warnings

        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await refresh_warnings(s, inv, org_settings={})
        await s.commit()
    (row,) = await _po_mismatch_rows(realdb, inv_id)
    assert row.status == "open"
    assert await _blocked(realdb, inv_id)
    return po_id, li, inv_id


@pytest.mark.asyncio
async def test_the_rest_of_the_delivery_lifts_the_hold_in_the_same_request(realdb):
    """Recorded by a manager who has nothing to do with the invoice — but the
    FIRST six were recorded by that same manager too, so neither receipt is
    implicated."""
    po_id, li, inv_id = await _held_invoice(realdb, uploaded_by=uuid.uuid4())
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["4"]))
    assert resp.status_code == 201
    (row,) = await _po_mismatch_rows(realdb, inv_id)
    assert row.status == "resolved"
    assert row.resolved_by == "PO match"
    assert not await _blocked(realdb, inv_id)


@pytest.mark.asyncio
async def test_the_uploader_cannot_release_their_own_invoice_with_a_receipt(realdb):
    """The clerk who keyed the invoice records the missing four units. The
    receipt lands — it may well be true — but it does not lift the hold on the
    invoice that clerk created; a person does."""
    clerk = realdb.info(TENANT).users["ap_clerk"]
    po_id, li, inv_id = await _held_invoice(realdb, uploaded_by=clerk)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["4"]))
    assert resp.status_code == 201
    (row,) = await _po_mismatch_rows(realdb, inv_id)
    assert row.status == "open"
    assert await _blocked(realdb, inv_id)


@pytest.mark.asyncio
async def test_the_segregation_opt_out_lets_the_uploaders_receipt_lift_it(realdb):
    """`settings.exceptions.require_segregation: false` lifts the receipt rule
    the same way it lifts the inspection rule."""
    from app.models.organization import Organization

    clerk = realdb.info(TENANT).users["ap_clerk"]
    async with realdb.control_sessionmaker()() as cs:
        org = (
            await cs.execute(
                select(Organization).where(Organization.id == realdb.info(TENANT).org_id)
            )
        ).scalar_one()
        org.settings = {**(org.settings or {}), "exceptions": {"require_segregation": False}}
        await cs.commit()
    po_id, li, inv_id = await _held_invoice(realdb, uploaded_by=clerk)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post("/api/goods-receipts", json=_body(po_id, [li], ["4"]))
    assert resp.status_code == 201
    (row,) = await _po_mismatch_rows(realdb, inv_id)
    assert row.status == "resolved"


@pytest.mark.asyncio
async def test_cancelling_a_receipt_raises_the_hold_again(realdb):
    """Ten of ten received, the full PO billed: clean. Cancel the only receipt
    and the same request raises the hold — nothing is recorded as received."""
    po_id, number, (li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        full = (await c.post("/api/goods-receipts", json=_body(po_id, [li], ["10"]))).json()
    inv_id = await _invoice(realdb, number)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        from app.services.invoice_warnings import refresh_warnings

        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await refresh_warnings(s, inv, org_settings={})
        await s.commit()
    assert await _po_mismatch_rows(realdb, inv_id) == []

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/goods-receipts/{full['id']}/cancel")
    assert resp.status_code == 200
    (row,) = await _po_mismatch_rows(realdb, inv_id)
    assert row.status == "open"
    assert await _blocked(realdb, inv_id)


@pytest.mark.asyncio
async def test_the_uploader_cannot_release_their_invoice_by_cancelling_a_receipt(realdb):
    """The money-path review's worked example. A manager records six of ten;
    the clerk's invoice for the full PO is held. The clerk cancels the
    manager's receipt — allowed, receiving is entry work. Nothing is now
    received, so the invoice bills beyond receipt and stays held. (Before the
    matcher read an all-cancelled PO as 3-way-with-zero, it fell back to a
    2-way match of 1,037 against 1,037 and the hold closed itself.)"""
    clerk = realdb.info(TENANT).users["ap_clerk"]
    po_id, li, inv_id = await _held_invoice(realdb, uploaded_by=clerk)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        receipts = (await c.get("/api/goods-receipts", params={"po_id": str(po_id)})).json()
    (receipt,) = receipts["items"]
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/goods-receipts/{receipt['id']}/cancel")
    assert resp.status_code == 200
    (row,) = await _po_mismatch_rows(realdb, inv_id)
    assert row.status == "open"
    assert await _blocked(realdb, inv_id)


@pytest.mark.asyncio
async def test_the_receipt_and_the_rematch_land_together(realdb, monkeypatch):
    """The rematch is not best-effort for an interactive receipt: if it fails,
    the receipt is rolled back with it rather than landing without the holds it
    should raise."""
    po_id, number, (li,) = await _po(realdb)
    await _invoice(realdb, number)

    async def boom(*_a, **_k):
        raise RuntimeError("matcher down")

    monkeypatch.setattr("app.services.invoice_warnings.refresh_warnings", boom)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        with pytest.raises(RuntimeError):
            await c.post("/api/goods-receipts", json=_body(po_id, [li], ["3"]))
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        assert (
            await s.execute(select(GoodsReceipt).where(GoodsReceipt.po_id == po_id))
        ).first() is None


def test_received_date_type_is_a_date():
    """Guard the schema's contract the form relies on (ISO date, not datetime)."""
    from app.schemas.goods_receipt import GoodsReceiptCreate

    parsed = GoodsReceiptCreate(
        po_id=uuid.uuid4(),
        received_date="2026-10-07",
        lines=[{"po_line_item_id": str(uuid.uuid4()), "quantity_received": "1.5"}],
    )
    assert parsed.received_date == date(2026, 10, 7)
    assert parsed.lines[0].quantity_received == Decimal("1.5")


# --------------------------------------------------------------------------- #
# Review follow-ups: replay edge cases, cancel scoping, legacy lines
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_reused_key_with_a_different_body_is_refused(realdb):
    """A replay must be the same request. The same key on the same PO with
    different quantities is not answered with the earlier receipt."""
    po_id, _, (li,) = await _po(realdb)
    key = str(uuid.uuid4())
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(
            "/api/goods-receipts", json=_body(po_id, [li], ["5"]), headers={"Idempotency-Key": key}
        )
        changed = await c.post(
            "/api/goods-receipts", json=_body(po_id, [li], ["6"]), headers={"Idempotency-Key": key}
        )
    assert first.status_code == 201
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "goods_receipt_idempotency_reused"


@pytest.mark.asyncio
async def test_a_key_racing_onto_another_po_is_a_409_not_a_500(realdb, monkeypatch):
    """Two concurrent submits with one key on two POs take two PO locks and
    both miss the lookup; the second lands on the unique index. Simulated by
    hiding the earlier receipt from the first lookup only."""
    from app.services import goods_receipts as svc

    po_a, _, (la,) = await _po(realdb)
    po_b, _, (lb,) = await _po(realdb)
    key = str(uuid.uuid4())
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (
            await c.post(
                "/api/goods-receipts",
                json=_body(po_a, [la], ["1"]),
                headers={"Idempotency-Key": key},
            )
        ).status_code == 201

        real = svc._by_idempotency_key
        calls = {"n": 0}

        async def first_lookup_misses(db, org_id, k):
            calls["n"] += 1
            return None if calls["n"] == 1 else await real(db, org_id, k)

        monkeypatch.setattr(svc, "_by_idempotency_key", first_lookup_misses)
        raced = await c.post(
            "/api/goods-receipts", json=_body(po_b, [lb], ["1"]), headers={"Idempotency-Key": key}
        )
    assert raced.status_code == 409
    assert raced.json()["detail"]["code"] == "goods_receipt_idempotency_reused"
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        assert (
            await s.execute(select(GoodsReceipt).where(GoodsReceipt.po_id == po_b))
        ).first() is None


@pytest.mark.asyncio
async def test_cancel_is_scoped_like_every_other_read(realdb):
    po_id, _, (li,) = await _po(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        gr = (await c.post("/api/goods-receipts", json=_body(po_id, [li], ["1"]))).json()
    async with realdb.client(key=TENANT, role=None) as c:
        anon = await c.post(f"/api/goods-receipts/{gr['id']}/cancel")
    async with realdb.client(key="b", role="admin") as c:
        cross_tenant = await c.post(f"/api/goods-receipts/{gr['id']}/cancel")

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        sibling = Entity(
            name=f"UK Ltd {uuid.uuid4().hex[:4]}",
            slug=f"uk-{uuid.uuid4().hex[:6]}",
            organization_id=realdb.info(TENANT).org_id,
        )
        s.add(sibling)
        await s.commit()
    async with realdb.client(key=TENANT, role="admin") as c:
        cross_entity = await c.post(
            f"/api/goods-receipts/{gr['id']}/cancel", headers={"X-Entity-ID": str(sibling.id)}
        )
    assert anon.status_code == 401
    assert cross_tenant.status_code == 404
    assert cross_entity.status_code == 404
    async with mk() as s:
        status = (
            await s.execute(
                select(GoodsReceipt.status).where(GoodsReceipt.id == uuid.UUID(gr["id"]))
            )
        ).scalar_one()
    assert status == "received"


@pytest.mark.asyncio
async def test_the_po_total_counts_receipt_lines_that_name_no_po_line(realdb):
    """A receipt written before 0109 has no `po_line_item_id`. The per-line
    figure cannot attribute it; the total — the figure the 3-way leg reads —
    still counts it."""
    po_id, _, (li,) = await _po(realdb)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        legacy = GoodsReceipt(
            gr_number=f"OLD-{uuid.uuid4().hex[:6]}",
            po_id=po_id,
            status="received",
            organization_id=realdb.info(TENANT).org_id,
            entity_id=await _default_entity(s),
        )
        s.add(legacy)
        await s.flush()
        s.add(GRLineItem(gr_id=legacy.id, description="Racket", quantity_received=Decimal("4")))
        await s.commit()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await c.post("/api/goods-receipts", json=_body(po_id, [li], ["2"]))
        po = (await c.get(f"/api/purchase-orders/{po_id}")).json()
    assert po["line_items"][0]["quantity_received"] == 2.0
    assert po["quantity_received_total"] == 6.0


# --------------------------------------------------------------------------- #
# The shared rematch helper's two modes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_best_effort_rematch_logs_and_carries_on(realdb, monkeypatch, caplog):
    """The QMS sync's mode: a matcher failure is logged, never raised, and the
    caller's own writes in the same transaction survive it."""
    from app.services import invoice_warnings

    po_id, number, _ = await _po(realdb)
    await _invoice(realdb, number)

    async def boom(*_a, **_k):
        raise RuntimeError("matcher down")

    monkeypatch.setattr(invoice_warnings, "refresh_warnings", boom)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        marker = GoodsReceipt(
            gr_number=f"KEEP-{uuid.uuid4().hex[:6]}",
            po_id=po_id,
            status="received",
            organization_id=realdb.info(TENANT).org_id,
            entity_id=await _default_entity(s),
        )
        s.add(marker)
        await s.flush()
        with caplog.at_level("WARNING"):
            await invoice_warnings.refresh_invoices_citing_pos(
                s,
                realdb.info(TENANT).org_id,
                {number},
                org_settings={},
                caller="t",
                best_effort=True,
            )
        await s.commit()
        with pytest.raises(RuntimeError):
            await invoice_warnings.refresh_invoices_citing_pos(
                s, realdb.info(TENANT).org_id, {number}, org_settings={}, caller="t"
            )
    assert "best-effort rematch skipped" in caplog.text
    async with mk() as s:
        assert (
            await s.execute(select(GoodsReceipt).where(GoodsReceipt.id == marker.id))
        ).first() is not None


@pytest.mark.asyncio
async def test_no_po_numbers_means_no_query(realdb, monkeypatch):
    from app.services import invoice_warnings

    async def boom(*_a, **_k):
        raise AssertionError("should not run")

    monkeypatch.setattr(invoice_warnings, "refresh_warnings", boom)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        await invoice_warnings.refresh_invoices_citing_pos(
            s, realdb.info(TENANT).org_id, {"", None}, org_settings={}, caller="t"
        )
