"""Critical-path HTTP/integration tests for the invoice lifecycle — the
single most-used flow clients touch daily.

Where `test_invoice_lifecycle.py` and `test_workflow_state_machine.py`
pin the state machine at the *unit* layer (mocked db + dispatch_audit),
this file drives the actual `/api/invoices/{id}/*` workflow endpoints
over the real ASGI app against a real Postgres tenant DB — proving the
request → auth/RBAC → service → DB-commit → audit-row chain the client
actually exercises:

  - approve (HTTP) persists `approved` + writes an immutable
    `invoice.approved` audit row carrying the digital signature
  - approve-with-corrections persists the corrected fields AND records
    a SOX field-diff (`details.changes`) in the audit row, with money
    serialised as string-Decimal (never float)
  - reject (HTTP) persists `rejected`, opens a `review_rejected`
    exception, and writes the audit row with the reason
  - send-to-erp (HTTP) transitions `approved → sending_to_erp` (202)
    and writes the audit row
  - an illegal transition is refused with 409 at the HTTP boundary and
    leaves the row + audit trail untouched (approve a `new` invoice;
    reject a `paid` invoice)
  - RBAC: an `ap_clerk` cannot approve (403) and the row stays put
  - tenant isolation: approving an invoice that lives in tenant B from
    tenant A is a 404 (the tenant-scoped DB never sees the other row)

All assertions read the committed row back through a fresh session, so
they prove durability + atomicity, not just the HTTP response shape.
These run against the opt-in `realdb` fixture (skips without `pnpm
db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog


async def _seed_invoice(
    mk,
    org_id,
    *,
    status: InvoiceStatus,
    amount: str = "500.00",
    number: str = "CP-001",
) -> uuid.UUID:
    """Insert one invoice directly (no workflow instance) and return its id.

    Deliberately leaves `uploaded_by_id` NULL so the segregation-of-duties
    guard in approve never fires (the actor is never the uploader), keeping
    the approval path deterministic.
    """
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="Critical Path Vendor",
                amount=Decimal(amount),
                currency="USD",
                status=status,
            )
        )
        await s.commit()
    return inv_id


# ---------------------------------------------------------------------------
# Approve (HTTP) — happy path persists + audits + signs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_endpoint_persists_status_and_writes_signed_audit_row(realdb):
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.ready_for_review
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.approved
        assert inv.approval_date is not None  # finalize stamped it
        assert inv.approved_by  # actor name recorded

        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == inv_id)
                    .where(AuditLog.action == "invoice.approved")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "approve must write exactly one invoice.approved audit row"
    details = rows[0].details or {}
    assert details.get("old_status") == "ready_for_review"
    assert details.get("new_status") == "approved"
    # Digital signature (SOX non-repudiation) is stamped on the row only when a
    # signing key is configured (empty key → signing skipped, by design — no
    # hardcoded fallback). pytest doesn't load .env.development (only main.py
    # does), so assert the contract precisely: signed iff a key is set.
    from app.config import settings

    if settings.approval_signing_key:
        assert "signature" in details, "approval audit row must carry the HMAC signature"
    else:
        assert "signature" not in details, "no key configured → must not fake a signature"


@pytest.mark.asyncio
async def test_approve_with_corrections_persists_fields_and_records_field_diff(realdb):
    """A reviewer corrects the extracted amount + vendor at approval time.
    The corrected values must persist on the invoice, and the audit row
    must carry a SOX field-diff with money as string-Decimal (never float).
    """
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"),
        info.org_id,
        status=InvoiceStatus.ready_for_review,
        amount="100.00",
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{inv_id}/approve",
            json={"vendor": "Corrected Vendor LLC", "amount": "250.50"},
        )
    assert resp.status_code == 200, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.vendor_name == "Corrected Vendor LLC"
        assert inv.amount == Decimal("250.50")

        row = (
            await s.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == inv_id)
                .where(AuditLog.action == "invoice.approved")
            )
        ).scalar_one()
    changes = (row.details or {}).get("changes") or {}
    assert "amount" in changes, "field-diff must record the corrected amount"
    assert "vendor_name" in changes
    # Money serialises as string-Decimal in the diff (invariant: never float).
    assert isinstance(changes["amount"]["new"], str)
    assert changes["amount"]["new"] == "250.50"
    assert changes["amount"]["old"] == "100.00"


# ---------------------------------------------------------------------------
# PII — the raw vendor tax id is never echoed in a response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_vendor_tax_id_masked_and_round_trip_safe(realdb):
    """The invoice response masks the raw `vendor_tax_id` to `***<last4>` (PII
    invariant), and a UI that echoes the masked value back on edit must NOT
    overwrite the stored raw value (the schema drops an echoed mask)."""
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(
            "/api/invoices",
            json={
                "vendor": "Masked Vendor Inc",
                "invoice_number": "MASK-001",
                "amount": "42.00",
                "vendor_tax_id": "12-3456789",
            },
        )
        assert created.status_code == 201, created.text
        inv_id = created.json()["id"]
        # Create response never echoes the raw tax id.
        assert created.json()["vendor_tax_id"] == "***6789"

        got = await c.get(f"/api/invoices/{inv_id}")
        assert got.json()["vendor_tax_id"] == "***6789"

        # Echo the masked value back on an unrelated edit.
        patched = await c.patch(
            f"/api/invoices/{inv_id}",
            json={"vendor_tax_id": "***6789", "description": "edited"},
        )
        assert patched.status_code == 200, patched.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(inv_id)))).scalar_one()
        # Raw value persisted on create and untouched by the masked round-trip.
        assert inv.vendor_tax_id == "12-3456789"
        assert inv.description == "edited"


# ---------------------------------------------------------------------------
# Reject (HTTP) — persists + opens exception + audits the reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_endpoint_persists_status_opens_exception_and_audits_reason(realdb):
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.ready_for_review
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{inv_id}/reject",
            json={"reason": "amount does not match the PO"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.rejected

        audit = (
            await s.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == inv_id)
                .where(AuditLog.action == "invoice.rejected")
            )
        ).scalar_one()
        assert (audit.details or {}).get("reason") == "amount does not match the PO"

        exc = (
            await s.execute(select(APException).where(APException.invoice_id == inv_id))
        ).scalar_one()
    assert exc.exception_type == "review_rejected"
    assert exc.description == "amount does not match the PO"


@pytest.mark.asyncio
async def test_reject_requires_a_reason(realdb):
    """The reject body's `reason` is mandatory (min_length=1) — an empty
    reject is a 422, never a silent rejection with no audit trail."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.ready_for_review
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/reject", json={"reason": ""})
    assert resp.status_code == 422

    # Invoice untouched.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
    assert inv.status == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Send-to-ERP (HTTP) — approved → sending_to_erp + 202 + audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_erp_transitions_and_audits(realdb):
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.approved
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "sending_to_erp"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.sending_to_erp
        audit = (
            await s.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == inv_id)
                .where(AuditLog.action == "invoice.erp_submitted")
            )
        ).scalar_one()
    assert (audit.details or {}).get("new_status") == "sending_to_erp"


# ---------------------------------------------------------------------------
# Illegal transitions refused at the HTTP boundary (409), row + trail intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_of_a_rejected_invoice_is_rejected_409_and_writes_no_audit(realdb):
    """`rejected → approved` is NOT in VALID_TRANSITIONS — a rejected
    invoice must be resubmitted (`rejected → ready_for_review`) before it
    can be approved. The endpoint must 409 and leave the row + audit trail
    untouched — no back-door approval that skips re-review.

    (Note: `new → approved` IS legal — the direct-approve / auto-approve
    path — so this test deliberately uses `rejected`, which is not.)
    """
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.rejected
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/approve")
    assert resp.status_code == 409, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.rejected
        assert inv.approval_date is None
        count = (
            await s.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == inv_id)
            )
        ).scalar_one()
    assert count == 0, "an illegal transition must not write an audit row"


@pytest.mark.asyncio
async def test_reject_of_a_paid_invoice_is_rejected_409(realdb):
    """`paid → rejected` is not in VALID_TRANSITIONS. Rejecting an
    already-paid invoice must 409 — not silently flip a settled payable
    back into the review queue."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.paid)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/reject", json={"reason": "too late"})
    assert resp.status_code == 409, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
    assert inv.status == InvoiceStatus.paid


# ---------------------------------------------------------------------------
# RBAC — an ap_clerk can read but cannot approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ap_clerk_cannot_approve_invoice_403(realdb):
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.ready_for_review
    )

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/approve")
    assert resp.status_code == 403, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
    assert inv.status == InvoiceStatus.ready_for_review, "denied approval must not mutate state"


# ---------------------------------------------------------------------------
# PATCH cannot change status (BUG 1) — status is a workflow transition, not an
# editable field. A bare setattr via PATCH used to bypass the state machine,
# segregation-of-duties, the approval thresholds, the CFO gate, the approval
# signature, and the immutable audit row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_cannot_jump_status_to_paid(realdb):
    """PATCHing `status: paid` onto a `new` invoice must NOT change the status
    (the field is no longer editable here — only the dedicated transition
    endpoints move status, through the state machine). Other fields in the same
    body still save; no transition audit row is written."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.new, amount="100.00"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(
            f"/api/invoices/{inv_id}",
            json={"status": "paid", "description": "edited note"},
        )
    # The request itself succeeds (it's a valid field edit) — status is just
    # ignored. A 422 would also be acceptable, but we ship the field edit.
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "new", "PATCH must not move status"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.new, "status must stay put"
        assert inv.description == "edited note", "the legitimate field edit must persist"

        # The only audit row may be `invoice.edited` (the field diff) — never a
        # transition row, and never one that flips status.
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.entity_id == inv_id))).scalars().all()
        )
    actions = {r.action for r in rows}
    assert "invoice.paid" not in actions
    assert "invoice.approved" not in actions
    for r in rows:
        d = r.details or {}
        # No audit row may record a status transition out of `new`.
        assert d.get("new_status") not in {"paid", "approved"}
        # The field diff must not carry a status change either.
        assert "status" not in (d.get("changes") or {})


@pytest.mark.asyncio
async def test_patch_self_approve_via_status_is_closed(realdb):
    """The self-approve-via-PATCH hole: a user PATCHes `status: approved` onto
    their own invoice. This must NOT approve it — status stays put, approved_by
    / approval_date stay None, and no `invoice.approved` audit row is written.
    (Segregation of duties + the signature live only on the approve endpoint;
    PATCH must never reach them.)"""
    info = realdb.info("a")
    actor_id = info.users["ap_manager"]

    # Seed an invoice this same user uploaded.
    inv_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=info.org_id,
                invoice_number="CP-SELF-APPROVE",
                vendor_name="Self Approve Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.ready_for_review,
                uploaded_by_id=actor_id,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/invoices/{inv_id}", json={"status": "approved"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready_for_review"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.ready_for_review, "PATCH must not approve"
        assert inv.approved_by is None, "no approval finalize ran"
        assert inv.approval_date is None

        approved_rows = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == inv_id)
                .where(AuditLog.action == "invoice.approved")
            )
        ).scalar_one()
    assert approved_rows == 0, "no invoice.approved audit row may come from PATCH"


# ---------------------------------------------------------------------------
# Tenant isolation — approving a tenant-B invoice from tenant A is a 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_approve_an_invoice_from_another_tenant(realdb):
    """The classic IDOR: an admin in tenant A holds a valid JWT, learns a
    tenant-B invoice id, and POSTs approve with the tenant-A header. The
    tenant-scoped DB chokepoint must never see the cross-tenant row → 404,
    and tenant B's invoice stays exactly as it was."""
    b_info = realdb.info("b")
    b_inv_id = await _seed_invoice(
        realdb.sessionmaker("b"), b_info.org_id, status=InvoiceStatus.ready_for_review
    )

    # Tenant-A client (A's slug + A's JWT) targeting B's invoice id.
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/invoices/{b_inv_id}/approve")
    assert resp.status_code == 404, resp.text

    # B's invoice is untouched, and no approval audit row leaked into B.
    mk_b = realdb.sessionmaker("b")
    async with mk_b() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == b_inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.ready_for_review
        count = (
            await s.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == b_inv_id)
            )
        ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# Create cannot inject a status (BUG: status injection) — every invoice must
# enter the workflow at `new`. A POST body claiming `status: approved` (or
# `paid`) used to mint an invoice already past the gate, bypassing extraction,
# segregation, the thresholds, the CFO gate, and the approval signature.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_cannot_inject_approved_status(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json={
                "vendor": "Injection Vendor",
                "invoice_number": "CP-INJECT-1",
                "amount": "9999.00",
                "currency": "USD",
                "status": "approved",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "new", "create must always start at `new`, never honour a status claim"

    # And the row that committed is `new`, with no approval finalize.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(body["id"])))
        ).scalar_one()
        assert inv.status == InvoiceStatus.new
        assert inv.approved_by is None
        assert inv.approval_date is None


@pytest.mark.asyncio
async def test_create_cannot_inject_paid_status(realdb):
    """The most dangerous variant: minting a `paid` invoice would skip the whole
    money path. Status is ignored; the invoice enters at `new`."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/invoices",
            json={
                "vendor": "Injection Vendor",
                "invoice_number": "CP-INJECT-2",
                "amount": "100.00",
                "currency": "USD",
                "status": "paid",
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "new"


# ---------------------------------------------------------------------------
# Financial fields are frozen once approved — the signed-off amount is what the
# payment run pays. Editing the amount / line items after sign-off (but before
# ERP-send, where IMMUTABLE_STATUSES already blocks everything) used to slip
# through: `approved` was missing from the lock set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_cannot_mutate_amount_after_approved(realdb):
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"),
        info.org_id,
        status=InvoiceStatus.approved,
        amount="100.00",
        number="CP-FROZEN-1",
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/invoices/{inv_id}", json={"amount": "9999.00"})
    assert resp.status_code == 409, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.amount == Decimal("100.00"), "approved amount must not change"


@pytest.mark.asyncio
async def test_patch_allows_nonfinancial_edit_after_approved(realdb):
    """Guard must be surgical: notes / metadata stay editable on an approved
    invoice; only money fields are frozen."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"),
        info.org_id,
        status=InvoiceStatus.approved,
        amount="100.00",
        number="CP-FROZEN-2",
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/invoices/{inv_id}", json={"notes": "approved-era note"})
    assert resp.status_code == 200, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.notes == "approved-era note"
        assert inv.amount == Decimal("100.00")


# ---------------------------------------------------------------------------
# PATCH optimistic-concurrency guard — `expected_updated_at` (persona-panel
# power-user finding). Two clerks editing the same invoice at once used to
# silently clobber each other: an unguarded full-object read-modify-write with
# last-write-wins and no warning. The guard mirrors If-Unmodified-Since: when
# the client supplies the `updated_at` it loaded alongside the invoice and the
# row has since moved, the PATCH is refused 409 instead of overwriting.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_stale_expected_updated_at_is_refused_409(realdb):
    """Two sequential PATCHes, the second carrying an `expected_updated_at`
    captured BEFORE the first PATCH landed (the two-clerks-editing-at-once
    scenario): the second is refused 409, and the invoice reflects only the
    first PATCH's change — never a silent clobber."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.new, number="CP-OPTIMISTIC-1"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        # Both editors "load" the invoice before either one saves.
        loaded = await c.get(f"/api/invoices/{inv_id}")
        assert loaded.status_code == 200, loaded.text
        stale_token = loaded.json()["updated_at"]
        assert stale_token, "InvoiceResponse.updated_at must be populated"

        first = await c.patch(
            f"/api/invoices/{inv_id}",
            json={"notes": "editor A's note", "expected_updated_at": stale_token},
        )
        assert first.status_code == 200, first.text
        assert first.json()["notes"] == "editor A's note"
        fresh_token = first.json()["updated_at"]
        assert fresh_token != stale_token, "a successful PATCH must advance updated_at"

        # Editor B never re-loaded — still holds the token from before A saved.
        second = await c.patch(
            f"/api/invoices/{inv_id}",
            json={"notes": "editor B's clobber attempt", "expected_updated_at": stale_token},
        )
        assert second.status_code == 409, second.text
        assert "modified since you loaded it" in second.json()["detail"]["message"]
        assert second.json()["detail"]["code"] == "invoice_stale_edit"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.notes == "editor A's note", "the stale second PATCH must not have applied"


@pytest.mark.asyncio
async def test_patch_fresh_expected_updated_at_succeeds(realdb):
    """The positive control: a re-loaded, current `expected_updated_at`
    applies normally — the guard only refuses a STALE token, never a correct
    one."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.new, number="CP-OPTIMISTIC-2"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        loaded = await c.get(f"/api/invoices/{inv_id}")
        current_token = loaded.json()["updated_at"]

        resp = await c.patch(
            f"/api/invoices/{inv_id}",
            json={"notes": "up to date", "expected_updated_at": current_token},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["notes"] == "up to date"


# ---------------------------------------------------------------------------
# Approval is bound to the version the approver loaded (decisions §263). An
# edit that landed between the approver's read and their click used to be
# approved unseen — for managers as well as clerks. The same token as the PATCH
# guard, refused with its own code because the remedy is a re-review.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_with_a_stale_version_is_refused_and_nothing_is_signed(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(
        mk, info.org_id, status=InvoiceStatus.ready_for_review, number="CP-APPROVE-STALE"
    )

    async with realdb.client(key="a", role="ap_manager") as approver:
        loaded = await approver.get(f"/api/invoices/{inv_id}")
        seen = loaded.json()["updated_at"]

        # Someone else changes the payable while the approver is reading it.
        async with realdb.client(key="a", role="admin") as editor:
            edit = await editor.patch(f"/api/invoices/{inv_id}", json={"amount": "9500.00"})
            assert edit.status_code == 200, edit.text

        resp = await approver.post(
            f"/api/invoices/{inv_id}/approve", json={"expected_updated_at": seen}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "invoice_stale_approval"

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.approved_by is None and inv.approval_date is None
        signed = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.correlation_id == inv.correlation_id,
                        AuditLog.action == "invoice.approved",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert signed == [], "a refused approval must write no approval row"


@pytest.mark.asyncio
async def test_approve_with_the_current_version_approves(realdb):
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"),
        info.org_id,
        status=InvoiceStatus.ready_for_review,
        number="CP-APPROVE-CURRENT",
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        seen = (await c.get(f"/api/invoices/{inv_id}")).json()["updated_at"]
        resp = await c.post(f"/api/invoices/{inv_id}/approve", json={"expected_updated_at": seen})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_approve_with_corrections_still_binds_the_loaded_version(realdb):
    """The token rides beside the corrections and is never applied as one: a
    current token plus a correction approves with the correction; a stale one
    applies neither."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(
        mk, info.org_id, status=InvoiceStatus.ready_for_review, number="CP-APPROVE-CORR"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        seen = (await c.get(f"/api/invoices/{inv_id}")).json()["updated_at"]
        resp = await c.post(
            f"/api/invoices/{inv_id}/approve",
            json={"expected_updated_at": seen, "description": "corrected at approval"},
        )
        assert resp.status_code == 200, resp.text

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.approved
        assert inv.description == "corrected at approval"


@pytest.mark.asyncio
async def test_patch_without_expected_updated_at_is_backward_compatible(realdb):
    """Omitting the field entirely (every caller that predates this guard)
    behaves exactly as before — no check runs, last write still wins. This is
    the documented, deliberate default: only an opted-in caller gets the
    guard."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"), info.org_id, status=InvoiceStatus.new, number="CP-OPTIMISTIC-3"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.patch(f"/api/invoices/{inv_id}", json={"notes": "no token A"})
        assert first.status_code == 200, first.text
        second = await c.patch(f"/api/invoices/{inv_id}", json={"notes": "no token B"})
        assert second.status_code == 200, second.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.notes == "no token B"


@pytest.mark.asyncio
async def test_line_items_frozen_after_approved(realdb):
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"),
        info.org_id,
        status=InvoiceStatus.approved,
        amount="100.00",
        number="CP-FROZEN-3",
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[
                {
                    "description": "padded line",
                    "quantity": "1",
                    "unit_price": "9999.00",
                    "total": "9999.00",
                }
            ],
        )
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# The PAYEE is frozen once approved, not just the amount. `_execute_single_payment`
# resolves the payee's `Vendor.bank_details` through `Invoice.vendor_id`, and
# PATCH re-runs `match_and_link_vendor` whenever the vendor name is re-saved —
# so re-typing `vendor` on an `approved` invoice used to silently re-point the
# payment at a different supplier's bank account, with no vendor row touched
# (bypassing the dual-control `VendorChangeRequest` BEC gate) and nothing
# downstream contradicting it (the approval signature covers the amount and the
# actor, never the payee).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_cannot_repoint_the_payee_after_approved(realdb):
    from app.models.vendor import Vendor

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    genuine_id, other_id, inv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=genuine_id,
                organization_id=info.org_id,
                name="Genuine Supplier CP",
                status="active",
            )
        )
        s.add(
            Vendor(
                id=other_id,
                organization_id=info.org_id,
                name="Other Payee CP",
                status="active",
            )
        )
        s.add(
            Invoice(
                id=inv_id,
                organization_id=info.org_id,
                invoice_number="CP-PAYEE-1",
                vendor_name="Genuine Supplier CP",
                vendor_id=genuine_id,
                amount=Decimal("5000.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/invoices/{inv_id}", json={"vendor": "Other Payee CP"})
    assert resp.status_code == 409, resp.text

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.vendor_id == genuine_id, "approved invoice must keep its payee"
        assert inv.vendor_name == "Genuine Supplier CP"


@pytest.mark.asyncio
async def test_patch_cannot_change_remit_to_after_approved(realdb):
    """The other payment-destination field — what the remittance advice falls
    back to when the vendor row carries no address, and the value the
    `fraud_bank_change` rule watches."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"),
        info.org_id,
        status=InvoiceStatus.approved,
        number="CP-PAYEE-2",
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/invoices/{inv_id}", json={"remit_to_address": "1 Attacker Way"})
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_patch_can_still_repoint_the_payee_before_approval(realdb):
    """The freeze is surgical: correcting the vendor on an invoice still in
    review is the normal path (and the supported way to resolve a legacy
    unlinked invoice), so it must keep working."""
    from app.models.vendor import Vendor

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    other_id, inv_id = uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=other_id,
                organization_id=info.org_id,
                name="Corrected Supplier CP",
                status="active",
            )
        )
        s.add(
            Invoice(
                id=inv_id,
                organization_id=info.org_id,
                invoice_number="CP-PAYEE-3",
                vendor_name="Mis-extracted Name CP",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.ready_for_review,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/invoices/{inv_id}", json={"vendor": "Corrected Supplier CP"})
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.vendor_id == other_id, "pre-approval vendor correction must still link"


@pytest.mark.asyncio
async def test_a_line_item_edit_moves_the_version_an_approval_is_bound_to(realdb):
    """Lines are financial content, and a re-coded or re-described line with
    the same totals changes no header column. The version still has to move,
    or an approver holding the pre-edit version approves the new lines unseen
    (decisions §263)."""
    info = realdb.info("a")
    inv_id = await _seed_invoice(
        realdb.sessionmaker("a"),
        info.org_id,
        status=InvoiceStatus.ready_for_review,
        amount="100.00",
        number="CP-LINES-VERSION",
    )
    line = {"description": "hosting", "quantity": "1", "unit_price": "100.00", "total": "100.00"}

    async with realdb.client(key="a", role="ap_manager") as approver:
        first = await approver.put(f"/api/invoices/{inv_id}/line-items", json=[line])
        assert first.status_code == 200, first.text
        seen = (await approver.get(f"/api/invoices/{inv_id}")).json()["updated_at"]

        async with realdb.client(key="a", role="admin") as editor:
            again = await editor.put(
                f"/api/invoices/{inv_id}/line-items",
                json=[{**line, "description": "consulting"}],
            )
            assert again.status_code == 200, again.text

        resp = await approver.post(
            f"/api/invoices/{inv_id}/approve", json={"expected_updated_at": seen}
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "invoice_stale_approval"
