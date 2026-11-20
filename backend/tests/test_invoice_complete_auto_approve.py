"""Real-Postgres coverage for `POST /api/invoices/{id}/complete`'s
amount-floor auto-approve path — specifically the money-threshold DISPLAY,
not just the decision.

Found via the frontend e2e suite after the workflow-schema discriminator fix
(#237) started correctly typing `ApprovalStepConfig.auto_approve_below` as
`Decimal` and the JSONB-write fix (workflow_definitions.py) started
serializing it as an exact string rather than a raw Decimal object. Both
fixes are individually correct (and covered elsewhere), but
`api/workflow.py::complete_invoice` read the threshold straight off the
frozen `steps_config_snapshot` and formatted it with an `f"{auto_below:,.2f}"`
spec that assumes a float/Decimal — a string threshold (which the JSONB now
legitimately stores) raises `ValueError: Unknown format code 'f' for object
of type 'str'`, turning a successful auto-approve into a 500.

`decide_auto_approve` itself was never affected — it already coerces via
`Decimal(str(auto_below))` before comparing. Only the success-message
f-string was unguarded. This is exactly the class of bug the near-identical
comment in `services/review.py` (`_enforce_approval_thresholds`) already
anticipates and guards against for `max_invoice_amount` / `require_cfo_above`
— `complete_invoice`'s own display line was the one spot that didn't.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus

TENANT = "a"


async def _set_auto_approve_below(realdb, *, workflow_id: str, threshold: str):
    async with realdb.client(key=TENANT, role="admin") as c:
        wf = (await c.get(f"/api/workflows/{workflow_id}")).json()
        steps = wf["steps_config"]["steps"]
        for step in steps:
            if step["type"] == "approval":
                step["enabled"] = True
                step["config"] = {
                    **step.get("config", {}),
                    "required": True,
                    "auto_approve_below": threshold,
                }
        resp = await c.patch(f"/api/workflows/{workflow_id}", json={"steps": steps})
        assert resp.status_code == 200, resp.text
        return steps


async def _get_active_workflow_id(realdb) -> str:
    async with realdb.client(key=TENANT, role="admin") as c:
        wfs = (await c.get("/api/workflows")).json()["items"]
    active = next(w for w in wfs if w["is_active"])
    return active["id"]


async def test_complete_auto_approves_and_formats_the_threshold_message(realdb):
    """The exact CI repro: PATCH the active workflow's approval step with a
    string auto_approve_below (mirrors what the JSONB now stores post-write),
    create an invoice under it as a DIFFERENT actor (segregation must not
    degrade this to human review), complete it, and assert a 200 with a
    correctly formatted money message — not a 500."""
    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]

    try:
        await _set_auto_approve_below(realdb, workflow_id=workflow_id, threshold="5000.00")

        # Created by ap_manager, completed by admin — different actors, so the
        # segregation-of-duties degrade doesn't mask the auto-approve path.
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            create_resp = await c.post(
                "/api/invoices",
                json={
                    "vendor": "Auto Approve Vendor",
                    "invoice_number": f"AA-{workflow_id[:8]}",
                    "amount": "1234.50",
                    "currency": "USD",
                },
            )
        assert create_resp.status_code == 201, create_resp.text
        invoice_id = create_resp.json()["id"]

        async with realdb.client(key=TENANT, role="admin") as c:
            complete_resp = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert complete_resp.status_code == 200, complete_resp.text
        body = complete_resp.json()
        assert body["status"] == "approved"
        # The floor is denominated in the org's REPORTING currency, so the
        # message names it instead of assuming dollars.
        assert "5,000.00 USD" in body["message"]

        mk = realdb.sessionmaker(TENANT)
        async with mk() as s:
            inv = await s.get(Invoice, invoice_id)
            assert inv.status == InvoiceStatus.approved
            assert inv.amount == Decimal("1234.50")
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


async def _set_extraction_confidence_bar(realdb, *, workflow_id: str, threshold: float):
    """Turn the extraction step's confidence auto-approve ON with the given bar,
    and leave the approval step with NO amount floor."""
    async with realdb.client(key=TENANT, role="admin") as c:
        wf = (await c.get(f"/api/workflows/{workflow_id}")).json()
        steps = wf["steps_config"]["steps"]
        for step in steps:
            if step["type"] == "extraction":
                step["enabled"] = True
                step["config"] = {
                    "auto_approve_enabled": True,
                    "auto_approve_threshold": threshold,
                }
            if step["type"] == "approval":
                step["enabled"] = True
                cfg = {**step.get("config", {}), "required": True}
                cfg.pop("auto_approve_below", None)
                step["config"] = cfg
        resp = await c.patch(f"/api/workflows/{workflow_id}", json={"steps": steps})
        assert resp.status_code == 200, resp.text


async def test_complete_never_auto_approves_on_the_extraction_confidence_bar(realdb):
    """`complete_invoice` hands `decide_auto_approve` a hardcoded
    `overall_confidence=0.0` — a sentinel meaning "there is no extraction result
    on this path", not a measurement. It also used to hand it the extraction
    step's config, and `auto_approve_threshold` is a schema-valid `0.0..1.0`
    float, so an org that dragged the confidence bar to `0` (meaning
    "auto-approve at any confidence", for the EXTRACTION path) made
    `0.0 >= 0.0` fire here instead: a $999,999 invoice auto-approved with **no
    amount floor configured at all**, stamped `reason: "below_threshold"` with a
    null threshold, and then 500-ing on the response message — *after* the
    commit, so the caller saw an error while the invoice was silently approved.

    The amount floor is the only trigger that means anything on this path."""
    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]

    try:
        await _set_extraction_confidence_bar(realdb, workflow_id=workflow_id, threshold=0.0)

        async with realdb.client(key=TENANT, role="ap_manager") as c:
            create_resp = await c.post(
                "/api/invoices",
                json={
                    "vendor": "No Floor Vendor",
                    "invoice_number": f"NOFLOOR-{workflow_id[:8]}",
                    "amount": "999999.00",
                    "currency": "USD",
                },
            )
        assert create_resp.status_code == 201, create_resp.text
        invoice_id = create_resp.json()["id"]

        async with realdb.client(key=TENANT, role="admin") as c:
            complete_resp = await c.post(f"/api/invoices/{invoice_id}/complete")
        # Not a 500, and not an auto-approval — it goes to a human.
        assert complete_resp.status_code == 200, complete_resp.text
        assert complete_resp.json()["status"] == "ready_for_review"

        mk = realdb.sessionmaker(TENANT)
        async with mk() as s:
            inv = await s.get(Invoice, invoice_id)
            assert inv.status == InvoiceStatus.ready_for_review
            assert inv.approved_by is None
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


async def test_complete_audit_row_records_the_threshold_as_an_exact_string(realdb):
    """The `invoice.auto_approved` row's `threshold` is the parsed floor as an
    exact decimal string — the same figure the comparison and the response
    message use, never a raw JSONB value or a null."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]

    try:
        await _set_auto_approve_below(realdb, workflow_id=workflow_id, threshold="5000.00")

        async with realdb.client(key=TENANT, role="ap_manager") as c:
            create_resp = await c.post(
                "/api/invoices",
                json={
                    "vendor": "Audit Threshold Vendor",
                    "invoice_number": f"AUDTH-{workflow_id[:8]}",
                    "amount": "100.00",
                    "currency": "USD",
                },
            )
        invoice_id = create_resp.json()["id"]
        async with realdb.client(key=TENANT, role="admin") as c:
            assert (await c.post(f"/api/invoices/{invoice_id}/complete")).status_code == 200

        mk = realdb.sessionmaker(TENANT)
        async with mk() as s:
            row = (
                (
                    await s.execute(
                        select(AuditLog).where(
                            AuditLog.action == "invoice.auto_approved",
                            AuditLog.entity_id == invoice_id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert row.details["reason"] == "below_threshold"
            assert row.details["threshold"] == "5000.00"
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


# ---------------------------------------------------------------------------
# The floor decides on a matched invoice (decisions §425). A refresh skips the
# PO match on `new`, so the floor used to approve an invoice citing a PO that
# was never matched — or did not exist — with no hold ever raised.
# ---------------------------------------------------------------------------


async def _purchase_order(realdb, *, total: str, vendor_name: str | None = None) -> str:
    """An open USD PO for ``total``, fully received (10 of 10), so a matching
    invoice clears 2- and 3-way. Returns its number. ``vendor_name`` names the
    vendor the API links an invoice to; the matcher confines the PO to it."""
    import uuid

    from sqlalchemy import select

    from app.models.entity import Entity
    from app.models.procurement import GoodsReceipt, GRLineItem, POLineItem, PurchaseOrder

    info = realdb.info(TENANT)
    number = f"PO-425-{uuid.uuid4().hex[:8]}"
    async with realdb.sessionmaker(TENANT)() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        vendor_id = None
        if vendor_name is not None:
            from app.models.vendor import Vendor

            vendor_id = (
                await s.execute(select(Vendor.id).where(Vendor.name == vendor_name))
            ).scalar_one_or_none()
            if vendor_id is None:
                vendor = Vendor(name=vendor_name, status="active", organization_id=info.org_id)
                s.add(vendor)
                await s.flush()
                vendor_id = vendor.id
        po = PurchaseOrder(
            po_number=number,
            vendor_id=vendor_id,
            total=Decimal(total),
            currency="USD",
            status="open",
            organization_id=info.org_id,
            entity_id=ent,
        )
        s.add(po)
        await s.flush()
        s.add(POLineItem(po_id=po.id, description="Widget", quantity=Decimal("10")))
        gr = GoodsReceipt(
            gr_number=f"GR-425-{uuid.uuid4().hex[:8]}",
            po_id=po.id,
            status="received",
            organization_id=info.org_id,
            entity_id=ent,
        )
        s.add(gr)
        await s.flush()
        s.add(GRLineItem(gr_id=gr.id, description="Widget", quantity_received=Decimal("10")))
        await s.commit()
    return number


FLOOR_VENDOR = "Floor PO Vendor"


async def _create_under_floor(realdb, *, po_number: str | None, amount: str) -> str:
    import uuid

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json={
                "vendor": FLOOR_VENDOR,
                "invoice_number": f"FLPO-{uuid.uuid4().hex[:10]}",
                "amount": amount,
                "currency": "USD",
                **({"po_number": po_number} if po_number else {}),
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _po_holds(realdb, invoice_id) -> list[str]:
    from sqlalchemy import select

    from app.models.exception import Exception as APException

    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (
                await s.execute(
                    select(APException.status)
                    .where(
                        APException.invoice_id == invoice_id,
                        APException.exception_type == "po_mismatch",
                    )
                    .order_by(APException.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _suppressed_reason(realdb, invoice_id):
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    async with realdb.sessionmaker(TENANT)() as s:
        row = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "invoice.submitted_for_review",
                        AuditLog.entity_id == invoice_id,
                    )
                )
            )
            .scalars()
            .one()
        )
    return (row.details or {}).get("auto_approve_suppressed")


@pytest.mark.parametrize("case", ["unknown_po", "over_billed_po"])
async def test_complete_floor_holds_an_invoice_whose_po_match_fails(realdb, case):
    """A below-floor invoice citing a PO that cannot be found, or one it bills
    23.7 % over, is matched before the floor decides: it lands at review with
    an open `po_mismatch`, no approval stamped, and the run builder refuses it."""
    from app.services.payment_runs import blocked_invoice_ids

    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]
    try:
        await _set_auto_approve_below(realdb, workflow_id=workflow_id, threshold="5000.00")
        if case == "unknown_po":
            po_number = "PO-DOES-NOT-EXIST-425"
        else:
            po_number = await _purchase_order(realdb, total="1000.00", vendor_name=FLOOR_VENDOR)
        invoice_id = await _create_under_floor(realdb, po_number=po_number, amount="1237.00")

        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ready_for_review"

        async with realdb.sessionmaker(TENANT)() as s:
            inv = await s.get(Invoice, invoice_id)
            assert inv.status == InvoiceStatus.ready_for_review
            assert inv.approved_by is None
            assert inv.approved_by_user_id is None
            assert inv.approval_date is None
            # The reviewer sees the match the floor was judged on.
            assert inv.po_match is not None
            assert inv.po_match["status"] == ("no_po" if case == "unknown_po" else "mismatch")
            assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}
        assert await _po_holds(realdb, invoice_id) == ["open"]
        assert await _suppressed_reason(realdb, invoice_id) == "po_match_hold"
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


async def test_complete_floor_sends_a_cumulatively_over_billed_invoice_to_review(realdb):
    """Each invoice fits the PO on its own, so the match raises no hold; with
    the other bill already citing the PO the two pass its total, and that
    warning is no hold either (decisions §469). The floor still stands aside:
    no unattended approval past it."""
    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]
    try:
        await _set_auto_approve_below(realdb, workflow_id=workflow_id, threshold="5000.00")
        po_number = await _purchase_order(realdb, total="1000.00", vendor_name=FLOOR_VENDOR)
        await _create_under_floor(realdb, po_number=po_number, amount="600.00")
        invoice_id = await _create_under_floor(realdb, po_number=po_number, amount="500.00")

        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ready_for_review"

        async with realdb.sessionmaker(TENANT)() as s:
            inv = await s.get(Invoice, invoice_id)
            assert inv.status == InvoiceStatus.ready_for_review
            assert inv.approved_by is None
            assert inv.approval_date is None
            assert "po_billed_beyond_total" in [w.get("code") for w in inv.warnings or []]
        assert await _po_holds(realdb, invoice_id) == []
        assert await _suppressed_reason(realdb, invoice_id) == "po_overbilled"
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


async def test_complete_floor_approves_an_invoice_whose_po_matches(realdb):
    """A clean match is no reason to stop the floor: the invoice auto-approves,
    with the match on the row and no hold."""
    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]
    try:
        await _set_auto_approve_below(realdb, workflow_id=workflow_id, threshold="5000.00")
        po_number = await _purchase_order(realdb, total="1234.50", vendor_name=FLOOR_VENDOR)
        invoice_id = await _create_under_floor(realdb, po_number=po_number, amount="1234.50")

        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"

        async with realdb.sessionmaker(TENANT)() as s:
            inv = await s.get(Invoice, invoice_id)
            assert inv.status == InvoiceStatus.approved
            assert inv.po_match is not None
            assert inv.po_match["status"] == "matched"
        assert await _po_holds(realdb, invoice_id) == []
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


async def test_complete_floor_without_a_po_is_unchanged(realdb):
    """No PO reference: no match runs, nothing is held, the floor approves as
    it always has."""
    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]
    try:
        await _set_auto_approve_below(realdb, workflow_id=workflow_id, threshold="5000.00")
        invoice_id = await _create_under_floor(realdb, po_number=None, amount="1237.00")

        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"
        async with realdb.sessionmaker(TENANT)() as s:
            inv = await s.get(Invoice, invoice_id)
            assert inv.po_match is None
        assert await _po_holds(realdb, invoice_id) == []
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


async def test_complete_floor_stands_aside_for_a_hold_its_caller_cleared(realdb):
    """The caller cleared the invoice's `po_mismatch` while not its approver,
    which §420 allows. The floor would make them the approver, and §422's
    re-judge would then raise the hold again right after the stamp — an
    `approved` invoice with an open hold. The floor asks first and sends the
    invoice to a person; the clearance is left as it was for that person."""
    from sqlalchemy import update

    workflow_id = await _get_active_workflow_id(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        original_steps = (await c.get(f"/api/workflows/{workflow_id}")).json()["steps_config"][
            "steps"
        ]
    try:
        await _set_auto_approve_below(realdb, workflow_id=workflow_id, threshold="5000.00")
        po_number = await _purchase_order(realdb, total="1000.00", vendor_name=FLOOR_VENDOR)
        invoice_id = await _create_under_floor(realdb, po_number=po_number, amount="1237.00")

        async with realdb.client(key=TENANT, role="admin") as c:
            first = await c.post(f"/api/invoices/{invoice_id}/complete")
            assert first.json()["status"] == "ready_for_review", first.text
        from sqlalchemy import select

        from app.models.exception import Exception as APException

        async with realdb.sessionmaker(TENANT)() as s:
            exc_id = (
                await s.execute(
                    select(APException.id).where(
                        APException.invoice_id == invoice_id,
                        APException.exception_type == "po_mismatch",
                    )
                )
            ).scalar_one()
        async with realdb.client(key=TENANT, role="admin") as c:
            cleared = await c.post(
                f"/api/exceptions/{exc_id}/resolve",
                json={"action": "resolve", "resolution": "Agreed the overage by phone."},
            )
            assert cleared.status_code == 200, cleared.text

        # Back to a draft (test setup: the reject → new loop, without its noise).
        async with realdb.sessionmaker(TENANT)() as s:
            await s.execute(
                update(Invoice).where(Invoice.id == invoice_id).values(status=InvoiceStatus.new)
            )
            await s.commit()

        async with realdb.client(key=TENANT, role="admin") as c:
            again = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "ready_for_review"
        async with realdb.sessionmaker(TENANT)() as s:
            inv = await s.get(Invoice, invoice_id)
            assert inv.approved_by_user_id is None
        assert await _po_holds(realdb, invoice_id) == ["resolved"]
    finally:
        async with realdb.client(key=TENANT, role="admin") as c:
            await c.patch(f"/api/workflows/{workflow_id}", json={"steps": original_steps})


async def test_po_match_hold_refusal_reads_an_open_hold_on_an_extracted_invoice(realdb):
    """The predicate extraction's auto-approve asks (decisions §425): after the
    refresh an extraction runs on a `pending` invoice citing an unknown PO, the
    open `po_mismatch` refuses the unattended approval; a clean PO does not."""
    import uuid

    from sqlalchemy import select

    from app.models.entity import Entity
    from app.services.invoice_warnings import (
        UNATTENDED_APPROVAL_PO_HOLD_OPEN,
        po_match_hold_refusal,
        refresh_warnings,
    )

    info = realdb.info(TENANT)
    clean_po = await _purchase_order(realdb, total="500.00")
    async with realdb.sessionmaker(TENANT)() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        verdicts = {}
        for label, po_number in (("unknown", "PO-NOPE-425"), ("clean", clean_po)):
            inv = Invoice(
                invoice_number=f"EXT-425-{uuid.uuid4().hex[:8]}",
                vendor_name="Extraction Vendor",
                amount=Decimal("500.00"),
                currency="USD",
                po_number=po_number,
                status="pending",
                organization_id=info.org_id,
                entity_id=ent,
            )
            s.add(inv)
            await s.flush()
            await refresh_warnings(s, inv, org_settings={})
            verdicts[label] = await po_match_hold_refusal(s, inv, approver_id=None, org_settings={})
        await s.rollback()
    assert verdicts == {"unknown": UNATTENDED_APPROVAL_PO_HOLD_OPEN, "clean": None}


def test_every_unattended_approval_asks_the_po_match_first():
    """`/complete`'s floor and extraction's auto-approve each call
    `po_match_hold_refusal` before `record_final_approval` in the same function
    (decisions §425), so neither leaves an invoice `approved` with a PO-match
    hold open."""
    import ast
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1] / "app"
    sites = {("api/workflow.py", "complete_invoice"), ("services/extraction.py", "run_extraction")}
    found = set()
    for rel, fn_name in sites:
        tree = ast.parse((app_root / rel).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef) or fn.name != fn_name:
                continue
            calls = [
                (n.lineno, n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
                for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
            ]
            stamps = [at for at, name in calls if name == "record_final_approval"]
            asks = [at for at, name in calls if name == "po_match_hold_refusal"]
            assert stamps and asks, (rel, fn_name)
            assert max(asks) < min(stamps), (rel, fn_name)
            found.add((rel, fn_name))
    assert found == sites
