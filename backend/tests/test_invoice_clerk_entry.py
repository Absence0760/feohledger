"""The AP clerk enters invoices; approval and payment stay out of reach.

`api/invoice_entry.py` opens the entry endpoints to `ap_clerk` — create,
upload, file attach/replace/remove, field + line-item edits, extract / reset
extraction, submit for review, resubmit, CSV import, and the entry-only bulk
status targets — and holds an entry-only caller to the pre-approval window.
Segregation of duties is unchanged: it sits at approval, keyed on
`uploaded_by_id`, which every entry path stamps with the clerk.

CSV import's clerk rules are pinned in `test_csv_import.py`; the file routes'
in `test_invoice_file_management.py`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.services.approval_chain import violates_segregation

PDF = ("invoice.pdf", b"%PDF-1.4 clerk entry", "application/pdf")


async def _clerk_create(c, number: str, amount: str = "500.00") -> str:
    resp = await c.post(
        "/api/invoices",
        json={"vendor": "Clerk Entry Vendor", "invoice_number": number, "amount": amount},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _row(realdb, invoice_id) -> Invoice:
    async with realdb.sessionmaker("a")() as s:
        return (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()


async def _force(realdb, invoice_id, *, status: InvoiceStatus, approved_by: str | None) -> None:
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        row.status = status
        row.approved_by = approved_by
        await s.commit()


async def test_clerk_upload_creates_an_invoice_stamped_with_the_clerk(realdb):
    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/invoices/upload", files={"file": PDF})
    assert resp.status_code == 202, resp.text
    row = await _row(realdb, resp.json()["id"])
    assert row.uploaded_by_id == clerk_id
    assert row.file_key


async def test_clerk_enters_and_submits_but_cannot_approve_and_a_manager_can(realdb):
    """The whole entry → approval hand-off. The clerk keys, codes and submits;
    the clerk's approve is refused (no `invoice.approve`); and the invoice
    carries the clerk as uploader, so even a clerk later granted approval could
    not sign off their own entry. A manager — not implicated — approves it."""
    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-FLOW-001")

        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"description": "coded"})
        assert edit.status_code == 200, edit.text

        lines = await c.put(
            f"/api/invoices/{invoice_id}/line-items",
            json=[{"line_number": 1, "description": "Widgets", "total": "500.00"}],
        )
        assert lines.status_code == 200, lines.text

        submit = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert submit.status_code == 200, submit.text
        assert submit.json()["status"] == "ready_for_review"

        approve = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
        assert approve.status_code == 403
        reject = await c.post(f"/api/invoices/{invoice_id}/reject", json={"reason": "no"})
        assert reject.status_code == 403

    row = await _row(realdb, invoice_id)
    assert row.status == InvoiceStatus.ready_for_review
    assert row.uploaded_by_id == clerk_id
    assert violates_segregation(row, clerk_id, {"require_segregation": True})

    async with realdb.client(key="a", role="ap_manager") as c:
        approve = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert approve.status_code == 200, approve.text
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved


async def test_clerk_edits_are_refused_once_the_invoice_is_approved(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-LOCK-001")
    await _force(realdb, invoice_id, status=InvoiceStatus.approved, approved_by="Approver")

    async with realdb.client(key="a", role="ap_clerk") as c:
        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"notes": "late"})
        assert edit.status_code == 403
        assert edit.json()["detail"]["code"] == "invoice_entry_window_closed"
        lines = await c.put(f"/api/invoices/{invoice_id}/line-items", json=[])
        assert lines.status_code == 403
        attach = await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        assert attach.status_code == 403
        complete = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert complete.status_code == 403

    # A manager keeps the metadata clean-up the `approved` window allows.
    async with realdb.client(key="a", role="ap_manager") as c:
        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"notes": "late"})
    assert edit.status_code == 200, edit.text
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved


async def test_an_approved_invoice_whose_erp_push_failed_is_outside_the_clerk_window(
    realdb, monkeypatch
):
    """`failed` is also where an APPROVED invoice lands when its ERP push
    fails. Re-extracting or re-keying it would rewrite signed-off content, so
    the window reads `approved_by`, not just the status."""
    dispatch = AsyncMock()
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", dispatch)
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-ERPFAIL-001")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
    await _force(realdb, invoice_id, status=InvoiceStatus.failed, approved_by="Approver")

    async with realdb.client(key="a", role="ap_clerk") as c:
        extract = await c.post(f"/api/invoices/{invoice_id}/extract")
        assert extract.status_code == 403
        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "1.00"})
        assert edit.status_code == 403
    dispatch.assert_not_awaited()
    row = await _row(realdb, invoice_id)
    assert row.status == InvoiceStatus.failed
    assert row.amount == Decimal("500.00")


async def test_clerk_can_extract_and_reset_a_never_approved_invoice(realdb, monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", dispatch)
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-EXTRACT-001")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201

        extract = await c.post(f"/api/invoices/{invoice_id}/extract")
        assert extract.status_code == 200, extract.text
        assert extract.json()["status"] == "pending"

        reset = await c.post(f"/api/invoices/{invoice_id}/reset-extraction")
        assert reset.status_code == 200, reset.text
        assert reset.json()["status"] == "failed"
    dispatch.assert_awaited_once()


async def test_clerk_complete_never_closes_an_approved_invoice(realdb):
    """`/complete` is several transitions; a clerk gets only submit-for-review.
    On an approved invoice it would be `→ done` / `→ sending_to_erp`."""
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "CLERK-DONE-001")
    await _force(realdb, invoice_id, status=InvoiceStatus.approved, approved_by="Approver")

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/complete")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "invoice_entry_window_closed"
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved


async def test_clerk_resubmits_a_rejected_invoice(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-RESUB-001")
        assert (await c.post(f"/api/invoices/{invoice_id}/complete")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        rej = await c.post(f"/api/invoices/{invoice_id}/reject", json={"reason": "wrong PO"})
        assert rej.status_code == 200, rej.text
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/resubmit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready_for_review"


@pytest.mark.parametrize("target", ["approved", "rejected", "done", "pending"])
async def test_clerk_bulk_status_refuses_non_entry_targets(realdb, target):
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, f"CLERK-BULK-{target}-{uuid.uuid4().hex[:6]}")
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [invoice_id], "status": target, "reason": "r"},
        )
    assert resp.status_code == 403, resp.text
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.new


async def test_clerk_bulk_submits_for_review(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        ids = [await _clerk_create(c, f"CLERK-BULK-RFR-{i}") for i in range(2)]
        resp = await c.post(
            "/api/invoices/bulk/status", json={"ids": ids, "status": "ready_for_review"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    for invoice_id in ids:
        assert (await _row(realdb, invoice_id)).status == InvoiceStatus.ready_for_review


async def test_clerk_still_cannot_delete_or_send_to_erp(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-NOT-001")
        assert (await c.delete(f"/api/invoices/{invoice_id}")).status_code == 403
        assert (
            await c.post("/api/invoices/bulk/delete", json={"ids": [invoice_id]})
        ).status_code == 403
        assert (await c.post(f"/api/invoices/{invoice_id}/send-to-erp")).status_code == 403
        assert (await c.post(f"/api/invoices/{invoice_id}/retry-erp")).status_code == 403
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.new


# ---------------------------------------------------------------------------
# Closing the paths from a clerk's edit to an approval nobody else gave
# ---------------------------------------------------------------------------


async def _patch_snapshot(realdb, invoice_id, *, approval_enabled=True, config=None):
    """Rewrite the invoice's OWN frozen workflow snapshot (never the live
    definition, which other tests share)."""
    from app.models.workflow import WorkflowInstance

    async with realdb.sessionmaker("a")() as s:
        inst = (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
            )
        ).scalar_one()
        snap = {"steps": [dict(st) for st in inst.steps_config_snapshot["steps"]]}
        for st in snap["steps"]:
            if st["type"] == "approval":
                st["enabled"] = approval_enabled
                st["config"] = {**st.get("config", {}), **(config or {})}
        inst.steps_config_snapshot = snap
        await s.commit()


async def test_clerk_submit_never_takes_the_amount_floor_auto_approve(realdb):
    """An invoice nobody uploaded (email intake / PEPPOL) has an empty
    segregation set, so the floor's segregation degrade cannot bind. A clerk
    who re-keyed its amount below the floor would otherwise have their own
    figures approved by submitting them."""
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "CLERK-FLOOR-001", amount="900.00")
    await _patch_snapshot(
        realdb, invoice_id, config={"auto_approve_below": "5000.00", "require_segregation": True}
    )
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        row.uploaded_by_id = None  # an intake-shaped row
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "10.00"})
        assert edit.status_code == 200, edit.text
        submit = await c.post(f"/api/invoices/{invoice_id}/complete")
    assert submit.status_code == 200, submit.text
    assert submit.json()["status"] == "ready_for_review"
    row = await _row(realdb, invoice_id)
    assert row.status == InvoiceStatus.ready_for_review
    assert row.approved_by is None


async def test_clerk_cannot_complete_where_the_workflow_has_no_approval_step(realdb):
    """With approval disabled `/complete` closes a `new` invoice straight to
    `done` — no approval at all. That is not an entry transition."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-NOAPPROVAL-001")
    await _patch_snapshot(realdb, invoice_id, approval_enabled=False)

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/complete")
    assert resp.status_code == 403
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.new


async def test_clerk_edit_refused_once_a_chain_level_has_signed(realdb):
    """`approved_by` is set only at FINAL approval; a level-1 sign-off on a
    multi-level chain survives an edit, so it would carry over to content its
    approver never saw. A clerk reworks it through reject instead."""
    from app.models.workflow import WorkflowInstance

    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-CHAIN-001")
        assert (await c.post(f"/api/invoices/{invoice_id}/complete")).status_code == 200
    async with realdb.sessionmaker("a")() as s:
        inst = (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
            )
        ).scalar_one()
        inst.state_data = {
            **(inst.state_data or {}),
            "approval_levels": {
                "current_level": 1,
                "levels": [
                    {"level": 0, "approvals": [{"user_id": str(uuid.uuid4())}]},
                    {"level": 1, "approvals": []},
                ],
            },
        }
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "1.00"})
        assert edit.status_code == 403
        assert edit.json()["detail"]["code"] == "invoice_entry_window_closed"
        lines = await c.put(f"/api/invoices/{invoice_id}/line-items", json=[])
        assert lines.status_code == 403
    assert (await _row(realdb, invoice_id)).amount == Decimal("500.00")


async def test_clerk_bulk_resubmits_rejected_and_skips_what_is_past_entry(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rejected = await _clerk_create(c, "CLERK-BULK-REJ-1")
        once_approved = await _clerk_create(c, "CLERK-BULK-REJ-2")
        extracting = await _clerk_create(c, "CLERK-BULK-PEND-1")
    await _force(realdb, rejected, status=InvoiceStatus.rejected, approved_by=None)
    await _force(realdb, once_approved, status=InvoiceStatus.rejected, approved_by="Approver")
    await _force(realdb, extracting, status=InvoiceStatus.pending, approved_by=None)

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [rejected, once_approved, extracting], "status": "ready_for_review"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 1
    assert {sk["id"] for sk in body["skipped"]} == {once_approved, extracting}
    assert (await _row(realdb, rejected)).status == InvoiceStatus.ready_for_review
    assert (await _row(realdb, once_approved)).status == InvoiceStatus.rejected
    assert (await _row(realdb, extracting)).status == InvoiceStatus.pending


async def test_clerk_bulk_sends_a_rejected_invoice_back_to_draft(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-BULK-NEW-1")
    await _force(realdb, invoice_id, status=InvoiceStatus.rejected, approved_by=None)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/invoices/bulk/status", json={"ids": [invoice_id], "status": "new"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.new
