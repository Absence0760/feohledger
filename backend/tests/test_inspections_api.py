"""Quality Inspection API — CRUD + entity scoping over the real ASGI app.

Drives ``/api/inspections`` through the realdb client: POST create (201),
GET list + detail, a bad ``result`` → 400, and ``X-Entity-ID`` list scoping
(one inspection per entity; scoped view sees its own, consolidated sees both).
"""

from __future__ import annotations

import uuid

import pytest


async def _create_entity(c, *, name: str, slug: str) -> str:
    resp = await c.post("/api/entities", json={"name": name, "slug": slug})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.parametrize("header_role", ["admin"])
async def test_create_and_get_inspection(realdb, header_role):
    async with realdb.client(key="a", role=header_role) as c:
        resp = await c.post(
            "/api/inspections",
            json={
                "inspection_number": "QI-100",
                "result": "partial",
                "inspector": "Sam Lee",
                "accepted_quantity": "8.0000",
                "rejected_quantity": "2.0000",
                "deviation_notes": "Minor scuffs",
            },
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["inspection_number"] == "QI-100"
        assert created["result"] == "partial"
        assert created["accepted_quantity"] == 8.0
        iid = created["id"]

        # Detail.
        detail = await c.get(f"/api/inspections/{iid}")
        assert detail.status_code == 200
        assert detail.json()["inspector"] == "Sam Lee"

        # List — the canonical `{items, total, page, page_size}` envelope.
        lst = await c.get("/api/inspections")
        assert lst.status_code == 200
        body = lst.json()
        assert {"items", "total", "page", "page_size"} <= body.keys()
        assert iid in {row["id"] for row in body["items"]}


async def test_create_inspection_writes_an_audit_row(realdb):
    """A hand-recorded quality inspection clears (or fails) the 4-way match
    gate on a real invoice — it must leave an append-only row (invariant #3),
    just like a QMS-synced one."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-AUDIT", "result": "pass"},
        )
    assert resp.status_code == 201, resp.text
    iid = resp.json()["id"]

    async with realdb.sessionmaker("a")() as s:
        rows = list(
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "quality_inspection.created")
                )
            ).scalars()
        )
    mine = [r for r in rows if str(r.entity_id) == iid]
    assert len(mine) == 1, rows
    assert mine[0].actor_id is not None
    assert mine[0].details["inspection_number"] == "QI-AUDIT"
    assert mine[0].details["result"] == "pass"

    # Migration 0105: a hand-typed verdict says so, and names who typed it —
    # the PO-match auto-close will not let it release a quality hold on an
    # invoice that person is implicated in.
    from app.models.quality_inspection import QualityInspection

    async with realdb.sessionmaker("a")() as s:
        row = await s.get(QualityInspection, uuid.UUID(iid))
    assert row.source == "manual"
    assert row.recorded_by_user_id == mine[0].actor_id


async def test_get_unknown_inspection_is_404(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/inspections/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_create_bad_result_is_400(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-BAD", "result": "rejected"},
        )
    assert resp.status_code == 400, resp.text


async def test_create_requires_role(realdb):
    # ap_clerk is neither admin nor ap_manager → 403.
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-RBAC", "result": "pass"},
        )
    assert resp.status_code == 403, resp.text


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_list_scopes_by_entity(realdb):
    async with realdb.client(key="a", role="admin") as c:
        us = await _create_entity(c, name="US Inc", slug="us")

        # One inspection under US, one under the default entity (no header).
        r_us = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-US", "result": "pass"},
            headers={"X-Entity-ID": us},
        )
        assert r_us.status_code == 201, r_us.text
        r_def = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-DEF", "result": "fail"},
        )
        assert r_def.status_code == 201, r_def.text

        # Scoped to US → only the US inspection. `total` counts the whole
        # SCOPED set, not the whole tenant.
        scoped = await c.get("/api/inspections", headers={"X-Entity-ID": us})
        assert {r["inspection_number"] for r in scoped.json()["items"]} == {"QI-US"}
        assert scoped.json()["total"] == 1

        # Consolidated (no header) → both.
        allv = await c.get("/api/inspections")
        assert {r["inspection_number"] for r in allv.json()["items"]} == {"QI-US", "QI-DEF"}
        assert allv.json()["total"] == 2


async def test_sync_refuses_when_no_qms_is_configured(realdb):
    """The manual sync must apply the same opt-in rule the sweep does.

    Without a `settings.qms` block, `get_qms_adapter(None)` resolves to the
    `mock` adapter — so one call used to persist its three fabricated fixtures
    (`QMS-INSP-001 pass / PO-1001` …) against the tenant's REAL purchase
    orders, clearing or failing the 4-way quality gate on real invoices with
    rows indistinguishable from genuine ones.
    """
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/inspections/sync")
        assert resp.status_code == 409, resp.text
        assert "qms" in resp.json()["detail"].lower()

        # And nothing from the mock fixture set landed.
        listing = await c.get("/api/inspections")
        assert not [
            r
            for r in listing.json()["items"]
            if str(r["inspection_number"]).startswith("QMS-INSP-")
        ]


# --------------------------------------------------------------------------- #
# The matching consequence (decisions §401)
#
# A hand-entered verdict re-matches the invoices citing its PO in the same
# request, as a goods receipt does: a `fail` raises the payment-blocking
# `quality_hold`, a later `pass` closes it — unless whoever typed the pass is
# implicated in the invoice (§262's rule) or approved it (§389's reverse order,
# applied to the fourth leg).
# --------------------------------------------------------------------------- #

TENANT = "a"


async def _default_entity(s):
    from sqlalchemy import select

    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _po_and_invoice(realdb, *, uploaded_by=None, lines=()):
    """A PO for 1,037.00 and an invoice billing all of it, refreshed once under
    the org's own (empty) settings, so it starts with no PO-match exception.
    Returns (po_id, line ids, invoice id)."""
    from decimal import Decimal

    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.procurement import POLineItem, PurchaseOrder
    from app.services.invoice_warnings import refresh_warnings

    number = f"PO-QI-{uuid.uuid4().hex[:6]}"
    async with realdb.sessionmaker(TENANT)() as s:
        entity = await _default_entity(s)
        po = PurchaseOrder(
            po_number=number,
            total=Decimal("1037.00"),
            currency="USD",
            status="open",
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity,
        )
        s.add(po)
        await s.flush()
        line_ids = []
        for desc, qty in lines:
            li = POLineItem(po_id=po.id, description=desc, quantity=Decimal(qty))
            s.add(li)
            await s.flush()
            line_ids.append(li.id)
        inv = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Racket Wholesale Co",
            amount=Decimal("1037.00"),
            currency="USD",
            po_number=number,
            status=InvoiceStatus.ready_for_review,
            uploaded_by_id=uploaded_by,
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity,
        )
        s.add(inv)
        await s.flush()
        await refresh_warnings(s, inv, org_settings={})
        await s.commit()
        return po.id, line_ids, inv.id


async def _quality_holds(realdb, invoice_id):
    from sqlalchemy import select

    from app.models.exception import Exception as APException

    async with realdb.sessionmaker(TENANT)() as s:
        return (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == invoice_id,
                        APException.exception_type == "quality_hold",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _blocked(realdb, invoice_id) -> bool:
    from app.services.payment_runs import blocked_invoice_ids

    async with realdb.sessionmaker(TENANT)() as s:
        return invoice_id in await blocked_invoice_ids(s, [invoice_id])


async def _inspect(realdb, role, *, result, po_id=None, gr_id=None):
    body = {"inspection_number": f"QI-{uuid.uuid4().hex[:8]}", "result": result}
    if po_id is not None:
        body["po_id"] = str(po_id)
    if gr_id is not None:
        body["gr_id"] = str(gr_id)
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.post("/api/inspections", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _held(realdb, *, uploaded_by=None):
    """An invoice whose PO an admin has just failed at inspection."""
    po_id, _, inv_id = await _po_and_invoice(realdb, uploaded_by=uploaded_by)
    assert await _quality_holds(realdb, inv_id) == []
    await _inspect(realdb, "admin", result="fail", po_id=po_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "open"
    assert await _blocked(realdb, inv_id)
    return po_id, inv_id


async def _approve(realdb, inv_id, role):
    """Approval gates on no payment-blocking exception, so the hold rides
    along; it is the payment run it stops."""
    from app.models.invoice import Invoice, InvoiceStatus

    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.post(f"/api/invoices/{inv_id}/approve", json={})
    assert resp.status_code == 200, resp.text
    async with realdb.sessionmaker(TENANT)() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.approved
        assert inv.approved_by_user_id == realdb.info(TENANT).users[role]


async def test_a_failed_inspection_holds_the_invoice_in_the_same_request(realdb):
    """The follow-up's first half: a typed-in `fail` used to raise nothing
    until something else refreshed the invoice. `_held` asserts the hold."""
    _, inv_id = await _held(realdb)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.severity == "error"


async def test_a_pass_by_someone_uninvolved_lifts_the_hold(realdb):
    po_id, inv_id = await _held(realdb)
    await _inspect(realdb, "ap_manager", result="pass", po_id=po_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "resolved"
    assert row.resolved_by == "PO match"
    assert not await _blocked(realdb, inv_id)


async def test_a_pass_by_the_invoices_uploader_does_not_lift_it(realdb):
    """§262's rule, now reachable from the inspection door: the verdict lands
    and is what the matcher reads, but it is not evidence that releases an
    invoice its recorder created."""
    manager = realdb.info(TENANT).users["ap_manager"]
    po_id, inv_id = await _held(realdb, uploaded_by=manager)
    await _inspect(realdb, "ap_manager", result="pass", po_id=po_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "open"
    assert await _blocked(realdb, inv_id)


async def test_the_approver_cannot_release_their_invoice_with_a_later_pass(realdb):
    """§401: the manager approves the held invoice, then types in a passing
    inspection. One person signed the invoice and passed the goods, so the
    hold stays for someone else — who releases it with their own verdict."""
    po_id, inv_id = await _held(realdb)
    await _approve(realdb, inv_id, "ap_manager")

    await _inspect(realdb, "ap_manager", result="pass", po_id=po_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "open"
    assert await _blocked(realdb, inv_id)

    await _inspect(realdb, "admin", result="pass", po_id=po_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "resolved"
    assert not await _blocked(realdb, inv_id)


async def test_the_approval_steps_opt_out_lets_the_approvers_pass_lift_it(realdb):
    """`require_segregation: false` on the approval step lifts the approver
    half, as it lifts §267 and §389: one policy, not three."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.workflow import WorkflowDefinition, WorkflowInstance
    from app.utils.dates import utc_today

    manager = realdb.info(TENANT).users["ap_manager"]
    po_id, inv_id = await _held(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        snapshot = {
            "steps": [
                {"type": "approval", "enabled": True, "config": {"require_segregation": False}}
            ]
        }
        definition = WorkflowDefinition(
            name=f"qi-optout-{uuid.uuid4().hex[:6]}",
            steps_config=snapshot,
            is_active=False,
            organization_id=realdb.info(TENANT).org_id,
        )
        s.add(definition)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=definition.id, invoice_id=inv_id, steps_config_snapshot=snapshot
            )
        )
        inv = await s.get(Invoice, inv_id)
        inv.status = InvoiceStatus.approved
        inv.approved_by = "Manager"
        inv.approval_date = utc_today()
        inv.approved_by_user_id = manager
        await s.commit()

    await _inspect(realdb, "ap_manager", result="pass", po_id=po_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "resolved"


async def test_a_receipt_scoped_inspection_rematches_the_receipts_po(realdb):
    """A body naming only the goods receipt still reaches the invoices on that
    receipt's PO — the matcher reads a receipt's own inspection first."""
    from app.utils.dates import utc_today

    po_id, (li,), inv_id = await _po_and_invoice(realdb, lines=(("Racket", "10"),))
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        gr = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [{"po_line_item_id": str(li), "quantity_received": "10"}],
            },
        )
    assert gr.status_code == 201, gr.text
    assert await _quality_holds(realdb, inv_id) == []

    await _inspect(realdb, "admin", result="fail", gr_id=gr.json()["id"])
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "open"


async def test_the_inspection_and_its_rematch_land_together(realdb, monkeypatch):
    """Not best-effort: a re-match that fails rolls the inspection back rather
    than leaving a verdict on file whose hold was never raised."""
    from sqlalchemy import select

    from app.models.quality_inspection import QualityInspection

    po_id, _, _ = await _po_and_invoice(realdb)

    async def boom(*_a, **_k):
        raise RuntimeError("matcher down")

    monkeypatch.setattr("app.services.invoice_warnings.refresh_warnings", boom)
    async with realdb.client(key=TENANT, role="admin") as c:
        with pytest.raises(RuntimeError):
            await c.post(
                "/api/inspections",
                json={"inspection_number": "QI-ROLLBACK", "result": "fail", "po_id": str(po_id)},
            )
    async with realdb.sessionmaker(TENANT)() as s:
        assert (
            await s.execute(select(QualityInspection).where(QualityInspection.po_id == po_id))
        ).first() is None


async def test_a_chain_level_approver_cannot_release_it_either(realdb):
    """The approver set is §389's: a level signed by id in the chain state,
    later levels still open, counts as an approval of the invoice."""
    from app.models.workflow import WorkflowDefinition, WorkflowInstance
    from app.services.approval_chain import CHAIN_STATE_KEY

    manager = realdb.info(TENANT).users["ap_manager"]
    po_id, inv_id = await _held(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        snapshot = {"steps": [{"type": "approval", "enabled": True, "config": {}}]}
        definition = WorkflowDefinition(
            name=f"qi-chain-{uuid.uuid4().hex[:6]}",
            steps_config=snapshot,
            is_active=False,
            organization_id=realdb.info(TENANT).org_id,
        )
        s.add(definition)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=definition.id,
                invoice_id=inv_id,
                steps_config_snapshot=snapshot,
                state_data={
                    CHAIN_STATE_KEY: {
                        "current_level": 1,
                        "levels": [
                            {"level": 0, "approvals": [{"user_id": str(manager)}]},
                            {"level": 1, "approvals": []},
                        ],
                    }
                },
            )
        )
        await s.commit()

    await _inspect(realdb, "ap_manager", result="pass", po_id=po_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "open"


async def _received(realdb):
    """A PO with one line, fully received by the admin, and its invoice."""
    from app.utils.dates import utc_today

    po_id, (li,), inv_id = await _po_and_invoice(realdb, lines=(("Racket", "10"),))
    async with realdb.client(key=TENANT, role="admin") as c:
        gr = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [{"po_line_item_id": str(li), "quantity_received": "10"}],
            },
        )
    assert gr.status_code == 201, gr.text
    return po_id, gr.json()["id"], inv_id


async def test_a_receipts_hold_lifts_only_on_a_pass_against_that_receipt(realdb):
    """The matcher reads the newest receipt's own inspection first, so the
    way through is a verdict on the same leg: the approver's receipt pass is
    refused, a PO-level pass beside the receipt's fail is never read, and an
    independent receipt pass lifts it (decisions §401)."""
    po_id, gr_id, inv_id = await _received(realdb)
    await _inspect(realdb, "admin", result="fail", gr_id=gr_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "open"
    await _approve(realdb, inv_id, "ap_manager")

    await _inspect(realdb, "ap_manager", result="pass", gr_id=gr_id)
    assert (await _quality_holds(realdb, inv_id))[0].status == "open"

    await _inspect(realdb, "admin", result="pass", po_id=po_id)
    assert (await _quality_holds(realdb, inv_id))[0].status == "open"

    await _inspect(realdb, "admin", result="pass", gr_id=gr_id)
    (row,) = await _quality_holds(realdb, inv_id)
    assert row.status == "resolved"
    assert not await _blocked(realdb, inv_id)


async def test_the_rematch_is_locked_and_judged_under_the_orgs_settings(realdb, monkeypatch):
    """`lock_invoices=True` is what serialises an inspection with an approval
    of the same invoice (§389's argument, §401), and the org's own settings are
    what let the refresh close a hold at all. Both are pinned here, since
    dropping either would leave every other test green."""
    import app.services.invoice_warnings as iw

    seen: list[dict] = []
    real = iw.refresh_invoices_citing_pos

    async def spy(*args, **kwargs):
        seen.append(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(iw, "refresh_invoices_citing_pos", spy)
    po_id, _, _ = await _po_and_invoice(realdb)
    await _inspect(realdb, "admin", result="fail", po_id=po_id)
    (call,) = seen
    assert call["lock_invoices"] is True
    assert call["caller"] == "inspections"
    assert call["org_settings"] is not None
    assert call.get("best_effort", False) is False


async def test_a_po_outside_the_callers_entity_is_a_404(realdb):
    """The matcher reads inspections with no entity filter and this door
    releases holds on the spot, so a verdict filed from one subsidiary
    against another's PO is refused — the same 404 as a PO that does not
    exist — and nothing is written."""
    from decimal import Decimal

    from sqlalchemy import select

    from app.models.entity import Entity
    from app.models.procurement import PurchaseOrder
    from app.models.quality_inspection import QualityInspection

    async with realdb.sessionmaker(TENANT)() as s:
        entity = Entity(
            name=f"UK Ltd {uuid.uuid4().hex[:4]}",
            slug=f"uk-{uuid.uuid4().hex[:6]}",
            organization_id=realdb.info(TENANT).org_id,
        )
        s.add(entity)
        await s.flush()
        sibling = str(entity.id)
        default = await _default_entity(s)
        po = PurchaseOrder(
            po_number=f"PO-QI-{uuid.uuid4().hex[:6]}",
            total=Decimal("10.00"),
            currency="USD",
            status="open",
            organization_id=realdb.info(TENANT).org_id,
            entity_id=uuid.UUID(sibling),
        )
        s.add(po)
        await s.commit()
        po_id = po.id

    async with realdb.client(key=TENANT, role="admin") as c:
        scoped = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-XENT", "result": "pass", "po_id": str(po_id)},
            headers={"X-Entity-ID": str(default)},
        )
        missing = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-NONE", "result": "pass", "po_id": str(uuid.uuid4())},
            headers={"X-Entity-ID": str(default)},
        )
        consolidated = await c.post(
            "/api/inspections",
            json={"inspection_number": "QI-CONS", "result": "pass", "po_id": str(po_id)},
        )
    assert scoped.status_code == 404, scoped.text
    assert missing.status_code == 404, missing.text
    assert scoped.json() == missing.json()
    async with realdb.sessionmaker(TENANT)() as s:
        assert (
            await s.execute(
                select(QualityInspection).where(QualityInspection.inspection_number == "QI-XENT")
            )
        ).first() is None
    # From the consolidated view the PO is in scope, and the row is booked to
    # the PO's own entity rather than the default one.
    assert consolidated.status_code == 201, consolidated.text
    async with realdb.sessionmaker(TENANT)() as s:
        row = await s.get(QualityInspection, uuid.UUID(consolidated.json()["id"]))
    assert str(row.entity_id) == sibling


async def test_a_receipt_on_another_po_is_refused(realdb):
    """The matcher reads a receipt's own inspection through the receipt, never
    through `po_id`, so a body pairing a receipt with a different PO would
    record a link nothing honours."""
    _, gr_id, _ = await _received(realdb)
    other_po, _, _ = await _po_and_invoice(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/inspections",
            json={
                "inspection_number": "QI-PAIR",
                "result": "pass",
                "po_id": str(other_po),
                "gr_id": gr_id,
            },
        )
    assert resp.status_code == 422, resp.text
