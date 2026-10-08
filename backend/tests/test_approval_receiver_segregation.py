"""Receiving ≠ approving: whoever hand-recorded a live goods receipt on an
invoice's PO cannot approve that invoice (decisions §267).

Receipt entry (§262) already stops an implicated person's receipt from
*releasing* a payment hold. This is the other half of the classic control: a
manager who counted the delivery in cannot then sign off the invoice billed
against it. Real-Postgres harness (`realdb`). Pinned here:

  * the refusal — 403 `approval_segregation_receiver`, on the single approve
    door, the bulk door (a per-row skip naming the rule) and the shared
    `review.approve_invoice` every other door (email / Slack / Teams links,
    mobile, the exception agents) goes through;
  * someone else may approve — the rule names the receiver, not the invoice;
  * what counts — live, hand-entered receipts on the invoice's PO (by
    `po_number` under the matcher's scope, or by the stored match's `po_ids`)
    in the invoice's own entity. A cancelled receipt, a receipt with no
    source, and a receipt booked in a sibling entity do not;
  * an approve-with-corrections that re-points `po_number` at a PO the
    approver received is refused, not read against the old PO;
  * the approval step's `require_segregation: false` opt-out lifts it;
  * the batch lookup is two statements whatever the batch size (no N+1);
  * receivers are NOT in `implicated_actors` — the set
    `receipts_clear_hold` measures a receipt's recorder against.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import GoodsReceipt, POLineItem, PurchaseOrder
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services.approval_chain import (
    APPROVAL_SEGREGATION_RECEIVER,
    implicated_actors,
    receipt_recorders,
    receipt_recorders_by_invoice,
    violates_receiving_segregation,
)
from app.utils.dates import utc_today
from tests.query_counter import QueryCounter

TENANT = "a"
pytestmark = pytest.mark.asyncio


async def _default_entity(s):
    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _po(realdb, *, entity_id=None):
    """A ten-unit PO. Returns (po_id, po_number, line_id)."""
    number = f"PO-RX-{uuid.uuid4().hex[:6]}"
    async with realdb.sessionmaker(TENANT)() as s:
        po = PurchaseOrder(
            po_number=number,
            total=Decimal("1037.00"),
            currency="USD",
            status="open",
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity_id or await _default_entity(s),
        )
        s.add(po)
        await s.flush()
        li = POLineItem(po_id=po.id, description="Wilson Pro Staff racket", quantity=Decimal("10"))
        s.add(li)
        await s.commit()
        return po.id, number, li.id


async def _receive(realdb, role, po_id, line_id, qty="10"):
    """Record a receipt through the API, as `role`. Returns the receipt id."""
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [{"po_line_item_id": str(line_id), "quantity_received": qty}],
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _invoice(realdb, po_number, *, po_match=None, approval_config=None):
    """A ready-for-review invoice billed against `po_number`, uploaded by
    nobody who approves here. With `approval_config`, it gets its own frozen
    workflow snapshot carrying that approval-step config."""
    async with realdb.sessionmaker(TENANT)() as s:
        inv = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Racket Wholesale Co",
            amount=Decimal("1037.00"),
            currency="USD",
            po_number=po_number,
            po_match=po_match,
            status=InvoiceStatus.ready_for_review,
            uploaded_by_id=uuid.uuid4(),
            organization_id=realdb.info(TENANT).org_id,
            entity_id=await _default_entity(s),
        )
        s.add(inv)
        await s.flush()
        if approval_config is not None:
            snapshot = {"steps": [{"type": "approval", "enabled": True, "config": approval_config}]}
            # Not default and not active: the org's live definition is untouched.
            definition = WorkflowDefinition(
                name=f"receiver-sod-{uuid.uuid4().hex[:6]}",
                steps_config=snapshot,
                is_active=False,
                organization_id=realdb.info(TENANT).org_id,
            )
            s.add(definition)
            await s.flush()
            s.add(
                WorkflowInstance(
                    definition_id=definition.id,
                    invoice_id=inv.id,
                    steps_config_snapshot=snapshot,
                )
            )
        await s.commit()
        return inv.id


async def _approve(realdb, role, invoice_id, **body):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(f"/api/invoices/{invoice_id}/approve", json=body)


async def _status(realdb, invoice_id):
    async with realdb.sessionmaker(TENANT)() as s:
        return (
            await s.execute(select(Invoice.status).where(Invoice.id == invoice_id))
        ).scalar_one()


# --------------------------------------------------------------------------- #
# The refusal, on every door
# --------------------------------------------------------------------------- #


async def test_the_receiver_cannot_approve_and_someone_else_can(realdb):
    po_id, number, li = await _po(realdb)
    await _receive(realdb, "ap_manager", po_id, li)
    inv_id = await _invoice(realdb, number)

    refused = await _approve(realdb, "ap_manager", inv_id)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == APPROVAL_SEGREGATION_RECEIVER
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review

    approved = await _approve(realdb, "admin", inv_id)
    assert approved.status_code == 200, approved.text
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


async def test_bulk_approve_skips_the_receivers_rows_and_names_the_rule(realdb):
    po_a, number_a, li_a = await _po(realdb)
    po_b, number_b, _ = await _po(realdb)
    await _receive(realdb, "ap_manager", po_a, li_a)
    received = await _invoice(realdb, number_a)
    clean = await _invoice(realdb, number_b)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(received), str(clean)], "status": "approved"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 1
    (skip,) = body["skipped"]
    assert skip["id"] == str(received)
    assert "recorded a goods receipt" in skip["reason"]
    assert await _status(realdb, received) == InvoiceStatus.ready_for_review
    assert await _status(realdb, clean) == InvoiceStatus.approved


async def test_the_shared_approval_door_refuses_the_receiver(realdb):
    """`review.approve_invoice` is what the email / Slack / Teams links, the
    mobile app and the exception agents all call — so they all refuse."""
    from app.services.review import approve_invoice

    po_id, number, li = await _po(realdb)
    await _receive(realdb, "ap_manager", po_id, li)
    inv_id = await _invoice(realdb, number)
    manager = realdb.info(TENANT).users["ap_manager"]
    async with realdb.sessionmaker(TENANT)() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        with pytest.raises(HTTPException) as refused:
            await approve_invoice(
                s, inv, actor_id=manager, actor_name="Manager", actor_roles={"ap_manager"}
            )
        await s.rollback()
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == APPROVAL_SEGREGATION_RECEIVER


async def test_a_correction_cannot_repoint_the_invoice_at_a_po_the_approver_received(realdb):
    _, clean_number, _ = await _po(realdb)
    po_id, received_number, li = await _po(realdb)
    await _receive(realdb, "ap_manager", po_id, li)
    inv_id = await _invoice(realdb, clean_number)

    refused = await _approve(realdb, "ap_manager", inv_id, po_number=received_number)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == APPROVAL_SEGREGATION_RECEIVER
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review
    # The correction was applied before the check refused — it must roll back
    # with the refusal, not survive it.
    async with realdb.sessionmaker(TENANT)() as s:
        stored = (
            await s.execute(select(Invoice.po_number).where(Invoice.id == inv_id))
        ).scalar_one()
    assert stored == clean_number


# --------------------------------------------------------------------------- #
# What counts as a receipt the approver recorded
# --------------------------------------------------------------------------- #


async def test_a_cancelled_receipt_vouches_for_nothing(realdb):
    po_id, number, li = await _po(realdb)
    receipt = await _receive(realdb, "ap_manager", po_id, li)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (await c.post(f"/api/goods-receipts/{receipt}/cancel")).status_code == 200
    inv_id = await _invoice(realdb, number)

    resp = await _approve(realdb, "ap_manager", inv_id)
    assert resp.status_code == 200, resp.text


async def test_receipts_with_no_source_or_in_a_sibling_entity_do_not_count(realdb):
    """A receipt with no `source` predates receipt entry — no app user typed
    it. One booked in another entity never feeds this invoice's 3-way leg."""
    manager = realdb.info(TENANT).users["ap_manager"]
    po_id, number, _ = await _po(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        sibling = Entity(
            name=f"Sibling {uuid.uuid4().hex[:4]}",
            slug=f"sib-{uuid.uuid4().hex[:6]}",
            organization_id=realdb.info(TENANT).org_id,
        )
        s.add(sibling)
        await s.flush()
        for source, entity_id in ((None, await _default_entity(s)), ("manual", sibling.id)):
            s.add(
                GoodsReceipt(
                    gr_number=f"GR-{uuid.uuid4().hex[:6]}",
                    po_id=po_id,
                    status="received",
                    source=source,
                    recorded_by_user_id=manager,
                    organization_id=realdb.info(TENANT).org_id,
                    entity_id=entity_id,
                )
            )
        await s.commit()
    inv_id = await _invoice(realdb, number)
    async with realdb.sessionmaker(TENANT)() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert await receipt_recorders(s, inv) == frozenset()


async def test_a_multi_po_match_counts_every_po_it_names(realdb):
    """The multi-PO split records a combined `po_number` that resolves to
    nothing; its `po_match.po_ids` is what names the POs."""
    _, number_a, _ = await _po(realdb)
    po_b, number_b, li_b = await _po(realdb)
    await _receive(realdb, "ap_manager", po_b, li_b)
    inv_id = await _invoice(
        realdb, f"{number_a},{number_b}", po_match={"status": "matched", "po_ids": [str(po_b)]}
    )
    resp = await _approve(realdb, "ap_manager", inv_id)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == APPROVAL_SEGREGATION_RECEIVER


async def test_the_approval_steps_segregation_opt_out_lifts_it(realdb):
    po_id, number, li = await _po(realdb)
    await _receive(realdb, "ap_manager", po_id, li)
    inv_id = await _invoice(realdb, number, approval_config={"require_segregation": False})
    resp = await _approve(realdb, "ap_manager", inv_id)
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Shape of the lookup
# --------------------------------------------------------------------------- #


async def test_the_batch_lookup_is_two_statements_whatever_the_batch(realdb):
    rows = []
    for _ in range(4):
        po_id, number, li = await _po(realdb)
        await _receive(realdb, "ap_manager", po_id, li)
        rows.append(await _invoice(realdb, number))
    manager = str(realdb.info(TENANT).users["ap_manager"])
    async with realdb.sessionmaker(TENANT)() as s:
        invoices = (await s.execute(select(Invoice).where(Invoice.id.in_(rows)))).scalars().all()
        with QueryCounter() as one:
            await receipt_recorders_by_invoice(s, invoices[:1])
        with QueryCounter() as four:
            found = await receipt_recorders_by_invoice(s, invoices)
    assert len(one.statements) == len(four.statements) == 2
    assert all(found[i] == frozenset({manager}) for i in rows)


async def test_receivers_are_not_implicated_in_the_payable(realdb):
    """`implicated_actors` is what `receipts_clear_hold` asks a receipt's
    recorder NOT to be in. Folding receivers into it would make every
    hand-entered receipt implicate its own recorder, and no receipt could lift
    a hold again — so the receiving rule is approval-only."""
    po_id, number, li = await _po(realdb)
    await _receive(realdb, "ap_manager", po_id, li)
    inv_id = await _invoice(realdb, number)
    manager = realdb.info(TENANT).users["ap_manager"]
    async with realdb.sessionmaker(TENANT)() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert str(manager) in await receipt_recorders(s, inv)
        assert str(manager) not in implicated_actors(inv)


def test_the_predicate_honours_the_opt_out_and_a_null_actor():
    actor = uuid.uuid4()
    recorders = {str(actor)}
    assert violates_receiving_segregation(actor, recorders, {})
    assert not violates_receiving_segregation(actor, recorders, {"require_segregation": False})
    assert not violates_receiving_segregation(None, recorders, {})
    assert not violates_receiving_segregation(uuid.uuid4(), recorders, {})
