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
from datetime import date
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


async def test_nobody_re_extracts_an_approved_invoice_whose_erp_push_failed(realdb, monkeypatch):
    """A manager is refused too: re-reading the document would rewrite content
    an approver signed while `approved_by` still names them."""
    dispatch = AsyncMock()
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", dispatch)
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "MGR-ERPFAIL-001")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
    await _force(realdb, invoice_id, status=InvoiceStatus.failed, approved_by="Approver")

    async with realdb.client(key="a", role="ap_manager") as c:
        extract = await c.post(f"/api/invoices/{invoice_id}/extract")
        assert extract.status_code == 409, extract.text
    dispatch.assert_not_awaited()
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.failed


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


async def test_clerk_cannot_change_an_invoice_once_it_is_submitted(realdb):
    """Approval binds to no version, so an edit landing between the approver's
    read and their click would be approved unseen — re-pointing the payee
    (`vendor` re-links `vendor_id`) is the dangerous one. The clerk's window
    closes at submit; a correction goes through reject → rework."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-SUBMITTED-001")
        assert (await c.post(f"/api/invoices/{invoice_id}/complete")).status_code == 200

        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"vendor": "Someone Else"})
        assert edit.status_code == 403
        assert edit.json()["detail"]["code"] == "invoice_entry_window_closed"
        assert (await c.put(f"/api/invoices/{invoice_id}/line-items", json=[])).status_code == 403
        attach = await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        assert attach.status_code == 403
    row = await _row(realdb, invoice_id)
    assert row.status == InvoiceStatus.ready_for_review
    assert row.vendor_name == "Clerk Entry Vendor"
    assert row.file_key is None

    # A manager's reach is unchanged: they still correct a submitted invoice.
    async with realdb.client(key="a", role="ap_manager") as c:
        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"notes": "checked"})
    assert edit.status_code == 200, edit.text


async def test_the_window_reads_approval_date_not_just_the_approver_name(realdb):
    """`approved_by` is a display name and is empty for a user with a blank
    `full_name`; an approved invoice whose ERP push then failed would read as
    never approved on the name alone."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-BLANKNAME-001")
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        row.status = InvoiceStatus.failed
        row.approved_by = ""
        row.approval_date = date(2026, 1, 2)
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        edit = await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "1.00"})
    assert edit.status_code == 403
    assert (await _row(realdb, invoice_id)).amount == Decimal("500.00")


# ---------------------------------------------------------------------------
# An entry-only caller's document never auto-approves
# ---------------------------------------------------------------------------


def _enable_extraction_on_upload(monkeypatch):
    """Report the extraction step enabled so `upload` reaches its dispatch,
    whatever the shared test tenant's workflow says."""
    from app.api import workflow as workflow_api

    real = workflow_api.is_step_enabled

    async def _is_step_enabled(db, org_id, step_type, **kwargs):
        if step_type == "extraction":
            return True
        return await real(db, org_id, step_type, **kwargs)

    monkeypatch.setattr(workflow_api, "is_step_enabled", _is_step_enabled)


async def test_clerk_upload_dispatches_extraction_with_auto_approve_suppressed(realdb, monkeypatch):
    """The unattended confidence / amount gates would otherwise approve a
    document the clerk chose, with nobody else involved."""
    dispatch = AsyncMock()
    monkeypatch.setattr("app.api.workflow.dispatch_extraction", dispatch)
    _enable_extraction_on_upload(monkeypatch)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/invoices/upload", files={"file": PDF})
    assert resp.status_code == 202, resp.text
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["suppress_auto_approve"] is True


async def test_manager_upload_keeps_touchless_auto_approve(realdb, monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr("app.api.workflow.dispatch_extraction", dispatch)
    _enable_extraction_on_upload(monkeypatch)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/invoices/upload", files={"file": PDF})
    assert resp.status_code == 202, resp.text
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["suppress_auto_approve"] is False


async def test_clerk_re_extraction_never_auto_approves(realdb, monkeypatch):
    """The laundering shape: an intake invoice nobody uploaded, a clerk swaps
    in a document of their choosing and re-extracts it. What the flag does
    inside `run_extraction` is pinned in `test_extraction_reextract_options.py`."""
    dispatch = AsyncMock()
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", dispatch)
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "CLERK-REEXTRACT-001")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
    await _force(realdb, invoice_id, status=InvoiceStatus.failed, approved_by=None)
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        row.uploaded_by_id = None  # an intake-shaped row
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        swap = await c.put(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        assert swap.status_code == 200, swap.text
        extract = await c.post(f"/api/invoices/{invoice_id}/extract")
    assert extract.status_code == 200, extract.text
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["suppress_auto_approve"] is True


# ---------------------------------------------------------------------------
# A manager's read of a document a clerk chose never auto-approves
# ---------------------------------------------------------------------------
#
# The dispatch flag above covers the clerk's own upload and extract. The read
# that would approve can be someone else's: a manager re-extracting after a
# clerk swapped the file in. These run the real `run_extraction` (mock adapter,
# MinIO) on a workflow that would auto-approve anything, so a pass that lands
# at review proves the suppression and nothing else.

_MOCK_EXTRACTION = {"extraction": {"program_type": "byok", "provider": "mock"}}
_APPROVE_ANYTHING = {
    "steps": [
        {
            "number": 1,
            "type": "extraction",
            "enabled": True,
            "config": {"auto_approve_enabled": True, "auto_approve_threshold": 0.0},
        },
        {"number": 2, "type": "approval", "enabled": True, "config": {"required": True}},
        {"number": 3, "type": "erp_export", "enabled": False, "config": {}},
    ]
}


async def _manager_reads(
    realdb, monkeypatch, invoice_id, *, during_read=None
) -> tuple[Invoice, dict]:
    """The manager's `POST /extract`, then the worker's pass exactly as the
    dispatcher would have run it. Returns the row and the completion audit.

    ``during_read`` runs once the worker has downloaded the file and before it
    decides — the window the provider call widens."""
    from app.models.workflow import AuditLog, WorkflowInstance
    from app.services.extraction import run_extraction

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inst = (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
            )
        ).scalar_one()
        inst.steps_config_snapshot = _APPROVE_ANYTHING
        await s.commit()

    dispatch = AsyncMock()
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", dispatch)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/extract")
    assert resp.status_code == 200, resp.text
    (_, _, actor_id), kwargs = dispatch.await_args
    # The manager's dispatch asks for nothing: what follows is run_extraction's own call.
    assert kwargs["suppress_auto_approve"] is False

    if during_read is not None:
        import app.services.storage as storage

        real_get = storage._get_object

        async def _get_after(key):
            data = await real_get(key)
            await during_read()
            return data

        monkeypatch.setattr(storage, "_get_object", _get_after)

    async with mk() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        await run_extraction(s, row, actor_id=actor_id, org_settings=_MOCK_EXTRACTION, **kwargs)
    async with mk() as s:
        audit = (
            await s.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == uuid.UUID(str(invoice_id)))
                .where(
                    AuditLog.action.in_(("invoice.auto_approved", "invoice.extraction_completed"))
                )
            )
        ).scalar_one()
    return await _row(realdb, invoice_id), audit


async def test_a_manager_cannot_auto_approve_a_document_a_clerk_swapped_in(realdb, monkeypatch):
    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "SWAP-AUTO-001")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
    async with realdb.client(key="a", role="ap_clerk") as c:
        swap = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("other.pdf", b"%PDF-1.4 swapped", "application/pdf")},
        )
    assert swap.status_code == 200, swap.text

    row, audit = await _manager_reads(realdb, monkeypatch, invoice_id)

    assert row.segregation_actor_ids == [str(clerk_id)]
    assert row.status is InvoiceStatus.ready_for_review
    assert row.approved_by is None and row.approval_date is None
    assert audit.action == "invoice.extraction_completed"
    assert audit.details["auto_approved"] is False
    assert audit.details["auto_approve_suppressed"] == "segregation_actors"


async def test_a_manager_cannot_auto_approve_a_clerks_own_upload_swap(realdb, monkeypatch):
    """A clerk replacing the file on their OWN upload is not stamped — they are
    already the uploader — so the set alone would have missed it."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "SWAP-AUTO-002")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
        swap = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("other.pdf", b"%PDF-1.4 swapped", "application/pdf")},
        )
    assert swap.status_code == 200, swap.text

    row, audit = await _manager_reads(realdb, monkeypatch, invoice_id)

    assert not row.segregation_actor_ids
    assert row.uploaded_by_id == realdb.info("a").users["ap_clerk"]
    assert row.status is InvoiceStatus.ready_for_review
    assert audit.details["auto_approve_suppressed"] == "uploaded_by_another_user"


async def test_a_clerk_swap_during_the_read_is_seen_before_approving(realdb, monkeypatch):
    """The worker's first look is unlocked and the AI call sits between it and
    the decision, while a `pending` invoice is still inside the clerk's entry
    window. A swap committed in that gap must reach the decision."""
    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "SWAP-AUTO-RACE")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201

    async def _clerk_swaps():
        async with realdb.client(key="a", role="ap_clerk") as c:
            # Same filename: the object is overwritten in place, `file_key` unchanged.
            swap = await c.put(
                f"/api/invoices/{invoice_id}/file",
                files={"file": ("invoice.pdf", b"%PDF-1.4 doctored", "application/pdf")},
            )
        assert swap.status_code == 200, swap.text

    row, audit = await _manager_reads(realdb, monkeypatch, invoice_id, during_read=_clerk_swaps)

    assert row.segregation_actor_ids == [str(clerk_id)]
    assert row.status is InvoiceStatus.ready_for_review
    assert audit.details["auto_approve_suppressed"] == "segregation_actors"


async def test_a_file_replaced_during_the_read_is_never_approved(realdb, monkeypatch):
    """A manager's own swap is not stamped, but the approval would attach a file
    nobody's read produced the figures from."""
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "SWAP-AUTO-RACE-2")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201

    async def _manager_swaps():
        async with realdb.client(key="a", role="ap_manager") as c:
            swap = await c.put(
                f"/api/invoices/{invoice_id}/file",
                files={"file": ("corrected.pdf", b"%PDF-1.4 corrected", "application/pdf")},
            )
        assert swap.status_code == 200, swap.text

    row, audit = await _manager_reads(realdb, monkeypatch, invoice_id, during_read=_manager_swaps)

    assert row.status is InvoiceStatus.ready_for_review
    assert audit.details["auto_approve_suppressed"] == "document_replaced_during_read"


async def test_a_managers_own_document_still_auto_approves(realdb, monkeypatch):
    """The control: the same workflow and read approve a document nobody else
    touched, so the two above are the suppression and not the setup."""
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "SWAP-AUTO-003")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201

    row, audit = await _manager_reads(realdb, monkeypatch, invoice_id)

    assert row.status is InvoiceStatus.approved
    assert row.approved_by == "system (auto-approve)"
    assert audit.action == "invoice.auto_approved"
    assert audit.details["auto_approve_suppressed"] is None


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


# ---------------------------------------------------------------------------
# An entry-only editor joins the invoice's segregation set
# ---------------------------------------------------------------------------


async def _intake_shaped(realdb, number: str) -> str:
    """An invoice nobody uploaded — the email-intake / PEPPOL shape."""
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, number)
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        row.uploaded_by_id = None
        await s.commit()
    return invoice_id


async def test_a_clerk_who_edits_someone_elses_invoice_can_never_approve_it(realdb):
    """With no uploader the segregation set was empty, so a clerk later given
    `invoice.approve` (a custom role, a promotion) could approve figures they
    keyed. Every content write stamps them — once."""
    clerk_id = realdb.info("a").users["ap_clerk"]
    invoice_id = await _intake_shaped(realdb, "CLERK-STAMP-001")

    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (
            await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "450.00"})
        ).status_code == 200
        assert (
            await c.put(
                f"/api/invoices/{invoice_id}/line-items",
                json=[{"line_number": 1, "description": "Widgets", "total": "450.00"}],
            )
        ).status_code == 200
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201

    row = await _row(realdb, invoice_id)
    assert row.uploaded_by_id is None
    assert row.segregation_actor_ids == [str(clerk_id)]
    assert violates_segregation(row, clerk_id, {"require_segregation": True})


@pytest.mark.parametrize("route", ["file_replace", "file_delete"])
async def test_a_clerk_swapping_the_source_document_is_stamped(realdb, route):
    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, f"CLERK-STAMP-{route}")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
    async with realdb.client(key="a", role="ap_clerk") as c:
        if route == "file_replace":
            resp = await c.put(
                f"/api/invoices/{invoice_id}/file",
                files={"file": ("other.pdf", b"%PDF-1.4 other", "application/pdf")},
            )
        else:
            resp = await c.delete(f"/api/invoices/{invoice_id}/file")
    assert resp.status_code == 200, resp.text
    assert (await _row(realdb, invoice_id)).segregation_actor_ids == [str(clerk_id)]


async def test_a_clerk_re_extracting_someone_elses_invoice_is_stamped(realdb, monkeypatch):
    """Re-extraction rewrites the vendor, amount, dates and lines — a content
    change, even with the document left as it was."""
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", AsyncMock())
    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_manager") as c:
        invoice_id = await _clerk_create(c, "CLERK-STAMP-EXTRACT-001")
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.post(f"/api/invoices/{invoice_id}/extract")).status_code == 200
    assert (await _row(realdb, invoice_id)).segregation_actor_ids == [str(clerk_id)]


async def test_bulk_status_reports_an_id_it_could_not_find(realdb):
    """The batch is entity-scoped and row-locked; an id it cannot load (gone, or
    in another entity) comes back as a skip, never a silent drop."""
    missing = str(uuid.uuid4())
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-BULK-MISSING-1")
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [invoice_id, missing], "status": "ready_for_review"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 1
    assert body["skipped"] == [{"id": missing, "reason": "invoice not found"}]


async def test_a_clerk_editing_their_own_entry_is_not_restamped(realdb):
    """The uploader column already names them; the set holds OTHER actors."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-STAMP-OWN-001")
        assert (
            await c.patch(f"/api/invoices/{invoice_id}", json={"notes": "own"})
        ).status_code == 200
    assert not (await _row(realdb, invoice_id)).segregation_actor_ids


async def test_a_managers_edit_is_not_stamped(realdb):
    """Approve-with-corrections is the approver editing what they sign; a
    manager's pre-review fix must not refuse that same manager the approval."""
    invoice_id = await _intake_shaped(realdb, "CLERK-STAMP-MGR-001")
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (
            await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "450.00"})
        ).status_code == 200
    assert not (await _row(realdb, invoice_id)).segregation_actor_ids


async def test_a_no_op_save_by_a_clerk_does_not_stamp(realdb):
    """Only a change is a preparer's act — echoing the stored values back is not."""
    invoice_id = await _intake_shaped(realdb, "CLERK-STAMP-NOOP-001")
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "500.00"})
    assert resp.status_code == 200, resp.text
    assert not (await _row(realdb, invoice_id)).segregation_actor_ids


async def test_clerk_entry_is_on_the_audit_trail_under_the_clerk(realdb):
    from app.models.workflow import AuditLog

    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_clerk") as c:
        invoice_id = await _clerk_create(c, "CLERK-AUDIT-001")
        assert (
            await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "510.00"})
        ).status_code == 200
        assert (
            await c.post(f"/api/invoices/{invoice_id}/file", files={"file": PDF})
        ).status_code == 201
    row = await _row(realdb, invoice_id)
    async with realdb.sessionmaker("a")() as s:
        actions = {
            a.action: a.actor_id
            for a in (
                await s.execute(
                    select(AuditLog).where(AuditLog.correlation_id == row.correlation_id)
                )
            )
            .scalars()
            .all()
        }
    for action in ("invoice.created", "invoice.edited", "invoice.file_attached"):
        assert actions.get(action) == clerk_id, (action, actions)


# ---------------------------------------------------------------------------
# Entry is a ROLE (`INVOICE_ENTRY_ROLES`), and approval a permission
# ---------------------------------------------------------------------------


async def _custom_role_client(realdb, *, permissions: list[str], with_clerk: bool = False):
    """A client for a fresh user in tenant A holding one custom role — and the
    system `ap_clerk` role too when `with_clerk`."""
    from app.api.deps import create_access_token
    from app.models.user import Role, User, UserRole
    from app.utils.passwords import pwd_context

    info = realdb.info("a")
    uid = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        role = Role(
            id=uuid.uuid4(),
            name=f"Custom {uuid.uuid4().hex[:8]}",
            description="clerk-entry test role",
            organization_id=info.org_id,
            permissions=permissions,
        )
        s.add(role)
        s.add(
            User(
                id=uid,
                email=f"{uuid.uuid4().hex[:10]}@clerk-entry.test",
                full_name="Custom Role User",
                hashed_password=pwd_context.hash("Passw0rd!xyz"),
                is_active=True,
                organization_id=info.org_id,
                must_change_password=False,
            )
        )
        await s.flush()
        s.add(UserRole(user_id=uid, role_id=role.id))
        if with_clerk:
            clerk_role = (
                await s.execute(
                    select(Role).where(Role.name == "ap_clerk", Role.organization_id.is_(None))
                )
            ).scalar_one()
            s.add(UserRole(user_id=uid, role_id=clerk_role.id))
        await s.commit()
    c = realdb.client(key="a", role=None)
    c.headers["Authorization"] = f"Bearer {create_access_token(uid, info.org_id)}"
    return c, uid


async def test_a_custom_role_alone_cannot_enter_invoices(realdb):
    """Entry is `INVOICE_ENTRY_ROLES`, not a catalog permission — a custom role
    confers it to no one, even one that can approve."""
    c, _ = await _custom_role_client(realdb, permissions=["invoice.approve"])
    async with c:
        resp = await c.post(
            "/api/invoices",
            json={"vendor": "V", "invoice_number": "CUSTOM-NOPE-001", "amount": "1.00"},
        )
        assert resp.status_code == 403
        assert (await c.post("/api/invoices/upload", files={"file": PDF})).status_code == 403


async def test_a_clerk_granted_approval_cannot_approve_what_they_edited(realdb):
    """The case the editor stamp exists for. `ap_clerk` plus a custom role
    granting `invoice.approve`, and no manage role: entry-only on the entry
    routes, yet able to call `/approve`. Their edit to an intake invoice (no
    uploader) stamps them, so their own approval of it is a segregation
    refusal rather than a self-approval of their own figures."""
    invoice_id = await _intake_shaped(realdb, "CUSTOM-BOTH-001")
    c, uid = await _custom_role_client(realdb, permissions=["invoice.approve"], with_clerk=True)
    async with c:
        assert (
            await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "10.00"})
        ).status_code == 200
        submit = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert submit.status_code == 200, submit.text
        assert submit.json()["status"] == "ready_for_review"
        approve = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert approve.status_code == 403, approve.text
    assert approve.json()["detail"]["code"] == "approval_segregation"
    row = await _row(realdb, invoice_id)
    assert row.status == InvoiceStatus.ready_for_review
    assert row.segregation_actor_ids == [str(uid)]
