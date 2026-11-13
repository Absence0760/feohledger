"""Supplier portal — revise & resubmit a rejected invoice.

persona-supplier finding (issue #328): a rejected invoice was a dead end — the
only way forward was uploading a brand-new file, which risks a duplicate flag
against the original and loses the thread. `POST /portal/invoices/{id}/resubmit`
swaps the file on the SAME row and resolves the open `review_rejected`
exception.

The corrected document is then RE-EXTRACTED (`docs/followups.md`), so the
reviewer no longer reconciles a new PDF against the stale fields from the
version they rejected. Two constraints make that safe and are pinned below:
`skip_vendor_match=True` (a fresh matching pass could re-link the invoice to a
different supplier and drop it out of this vendor's own portal list) and
`suppress_auto_approve=True` (a human rejected this document; its replacement
must not be approved unattended). With the extraction step disabled the invoice
still goes straight to `ready_for_review`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.api.deps import create_vendor_access_token
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog, WorkflowDefinition

TENANT = "a"
PDF = ("fix.pdf", b"%PDF-1.4\n corrected invoice \n%%EOF", "application/pdf")


async def _seed(mk, org_id):
    vendor_id, vu_id = uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name="Resubmit Co",
                organization_id=org_id,
                status="active",
                source="manual",
            )
        )
        s.add(
            VendorUser(
                id=vu_id,
                vendor_id=vendor_id,
                email=f"{vu_id}@portal.test",
                full_name="Portal User",
                hashed_password="x",
                is_active=True,
            )
        )
        await s.commit()
    return vendor_id, vu_id


async def _add_invoice(mk, org_id, vendor_id, *, status, number="RS-1", file_key=None) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=number,
                vendor_name="Resubmit Co",
                vendor_id=vendor_id,
                amount=Decimal("10.00"),
                currency="USD",
                status=status,
                file_key=file_key or f"{org_id}/{inv_id}/original.pdf",
                organization_id=org_id,
            )
        )
        await s.commit()
    return inv_id


async def _add_rejection_exc(mk, org_id, inv_id) -> uuid.UUID:
    exc_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            APException(
                id=exc_id,
                invoice_id=inv_id,
                exception_type="review_rejected",
                severity="warning",
                description="Wrong PO number",
                status="open",
                organization_id=org_id,
            )
        )
        await s.commit()
    return exc_id


def _no_extraction():
    """Force the resubmit down the extraction-disabled branch.

    The realdb harness builds tenants with `create_all`, not `provision_tenant`,
    so a tenant with no `WorkflowDefinition` falls back to the fail-closed
    `DEFAULT_STEPS_CONFIG` (extraction OFF). `_enable_extraction` below seeds the
    real provisioned pipeline for the tests that need the other branch.
    """
    return patch("app.api.portal.is_step_enabled", AsyncMock(return_value=False))


async def _enable_extraction(mk, org_id) -> None:
    """Seed the workflow every freshly provisioned tenant really starts on, so
    `is_step_enabled` is exercised for real rather than mocked."""
    from app.services.tenant_provisioning import PROVISIONED_STEPS_CONFIG

    async with mk() as s:
        s.add(
            WorkflowDefinition(
                name="Default",
                steps_config=PROVISIONED_STEPS_CONFIG,
                is_active=True,
                is_default=True,
                organization_id=org_id,
            )
        )
        await s.commit()


def _client(realdb, vu_id, vendor_id):
    token = create_vendor_access_token(vu_id, vendor_id)
    c = realdb.client(key=TENANT, role=None)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


@pytest.mark.asyncio
async def test_resubmit_without_extraction_goes_straight_back_to_review(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected)
    exc_id = await _add_rejection_exc(mk, org_id, inv_id)

    with _no_extraction():
        async with _client(realdb, vu_id, vendor_id) as client:
            resp = await client.post(f"/api/portal/invoices/{inv_id}/resubmit", files={"file": PDF})
    assert resp.status_code == 202, resp.text
    assert resp.json()["id"] == str(inv_id)
    assert resp.json()["status"] == "ready_for_review"

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.ready_for_review
        # Same row — the invoice stays this vendor's, keeps its number.
        assert inv.vendor_id == vendor_id
        assert inv.invoice_number == "RS-1"
        assert inv.file_key and inv.file_key.endswith("fix.pdf")

        exc = (await s.execute(select(APException).where(APException.id == exc_id))).scalar_one()
        assert exc.status == "resolved"

        audit_rows = (await s.execute(select(AuditLog))).scalars().all()
        actions = {r.action for r in audit_rows}
    assert "invoice.resubmitted_by_vendor" in actions
    # The rejected version is kept, not deleted, and its audit row names it
    # (decisions §363).
    resubmitted = next(r for r in audit_rows if r.action == "invoice.resubmitted_by_vendor")
    assert resubmitted.details["previous_file_key"] == f"{org_id}/{inv_id}/original.pdf"


@pytest.mark.asyncio
async def test_resubmit_does_not_re_read_an_invoice_ap_once_approved(realdb):
    """An invoice AP approved, then recalled (decisions §374) and rejected for
    the supplier to reissue, is not re-extracted even with the extraction step
    on: a re-read would rewrite signed-off figures, and the engine refuses
    `→ pending` for it. The corrected document goes straight to review."""
    from datetime import date

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed(mk, org_id)
    await _enable_extraction(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected)
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        inv.approved_by = "Original Approver"
        inv.approval_date = date(2026, 11, 1)
        await s.commit()
    await _add_rejection_exc(mk, org_id, inv_id)

    dispatch = AsyncMock()
    with patch("app.api.portal.dispatch_extraction", dispatch):
        async with _client(realdb, vu_id, vendor_id) as client:
            resp = await client.post(f"/api/portal/invoices/{inv_id}/resubmit", files={"file": PDF})

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "ready_for_review"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resubmit_re_extracts_the_corrected_document(realdb):
    """The invoice takes the documented `rejected → new → pending` rework loop
    and hands the corrected file to extraction with BOTH guards on. Without the
    re-extraction the reviewer sees the new PDF beside the rejected version's
    numbers; without the guards, re-extraction can move the payee or approve the
    replacement with no human in the loop."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed(mk, org_id)
    await _enable_extraction(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected)
    exc_id = await _add_rejection_exc(mk, org_id, inv_id)

    dispatch = AsyncMock()
    with patch("app.api.portal.dispatch_extraction", dispatch):
        async with _client(realdb, vu_id, vendor_id) as client:
            resp = await client.post(f"/api/portal/invoices/{inv_id}/resubmit", files={"file": PDF})

    assert resp.status_code == 202, resp.text
    # `run_extraction` performs the final `pending → ready_for_review`.
    assert resp.json()["status"] == "pending"

    dispatch.assert_awaited_once()
    args, kwargs = dispatch.await_args
    assert args[0] == inv_id
    assert kwargs == {"skip_vendor_match": True, "suppress_auto_approve": True}

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.pending
        assert inv.vendor_id == vendor_id
        assert inv.file_key and inv.file_key.endswith("fix.pdf")

        exc = (await s.execute(select(APException).where(APException.id == exc_id))).scalar_one()
        assert exc.status == "resolved"

        actions = [r.action for r in (await s.execute(select(AuditLog))).scalars().all()]
    # Two honest rows: the vendor's resubmission, then the extraction dispatch.
    assert "invoice.resubmitted_by_vendor" in actions
    assert "invoice.extraction_dispatched" in actions


@pytest.mark.asyncio
async def test_resubmit_dispatches_only_after_the_commit(realdb):
    """The extraction worker opens its OWN session — dispatching before the
    commit would hand it the pre-commit row (the old file key, the old status)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed(mk, org_id)
    await _enable_extraction(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected)

    observed: dict = {}

    async def _observe(invoice_id, org, actor, **kwargs):
        async with mk() as s:
            inv = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
            observed["status"] = inv.status
            observed["file_key"] = inv.file_key

    with patch("app.api.portal.dispatch_extraction", _observe):
        async with _client(realdb, vu_id, vendor_id) as client:
            resp = await client.post(f"/api/portal/invoices/{inv_id}/resubmit", files={"file": PDF})

    assert resp.status_code == 202, resp.text
    assert observed["status"] == InvoiceStatus.pending
    assert observed["file_key"].endswith("fix.pdf")


@pytest.mark.asyncio
async def test_resubmit_409s_unless_rejected(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.approved)

    async with _client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/invoices/{inv_id}/resubmit", files={"file": PDF})
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_resubmit_cannot_touch_another_vendors_invoice(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    mine_vendor, mine_vu = await _seed(mk, org_id)

    other_vendor = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=other_vendor,
                name="Other",
                organization_id=org_id,
                status="active",
                source="manual",
            )
        )
        await s.commit()
    their_inv = await _add_invoice(
        mk, org_id, other_vendor, status=InvoiceStatus.rejected, number="RS-OTHER"
    )

    async with _client(realdb, mine_vu, mine_vendor) as client:
        resp = await client.post(f"/api/portal/invoices/{their_inv}/resubmit", files={"file": PDF})
    assert resp.status_code == 404, resp.text
