"""A payment held at `pending_compliance` must be surfaced and resolvable.

`check_payment_compliance`'s own docstring promises a `hold` verdict "opens
an exception for AP review" — none of the four call sites in
`_execute_single_payment` actually did, so a held payment was invisible
everywhere except its own `failure_reason` field, and nothing could ever
move it forward: `/resume` explicitly skips `pending_compliance` rows, and
no endpoint let AP retry, release, or dismiss one. Found by exploratory
persona-driven testing (payment-processor persona). Filed as #235.

Covers, end to end over the real HTTP endpoints:
  - a hold (via the deterministic "no screenable vendor" trigger — an
    approved invoice with `vendor_id=NULL`) opens a `payment_compliance_hold`
    Exception, visible via `GET /api/exceptions`
  - `POST /payments/{id}/compliance/dismiss` flips the payment to `failed`
    and resolves the exception, without ever reaching the adapter
  - `POST /payments/{id}/compliance/release` re-runs compliance-then-adapter;
    once the invoice has a real (unblocked) vendor attached, the hold clears,
    the payment settles, and the exception resolves
  - `/release` and `/dismiss` both 409 on a payment that isn't
    `pending_compliance` (RBAC / guard coverage, not just the happy path)
  - a `/release` that SETTLES hands off to the ERP sync (the only path that
    marks the invoice `paid`), and one that stays held does not

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.workflow import AuditLog

pytestmark = pytest.mark.asyncio

TENANT = "a"


async def _seed_unscreenable_invoice(mk, org_id, *, number: str, amount: str) -> uuid.UUID:
    """An approved invoice with NO vendor link — `_execute_single_payment`'s
    deterministic, no-mock-config-needed hold trigger ("we cannot screen a
    payee we don't have")."""
    invoice_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=invoice_id,
                invoice_number=number,
                vendor_name="Unlinked Vendor Co",
                vendor_id=None,
                amount=Decimal(amount),
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                invoice_date=date.today(),
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return invoice_id


async def _create_and_execute_run(client, exec_client, invoice_id: uuid.UUID) -> str:
    create_resp = await client.post(
        "/api/payments/runs",
        json={"items": [{"invoice_id": str(invoice_id), "method": "ach"}]},
    )
    assert create_resp.status_code == 201, create_resp.text
    run_id = create_resp.json()["id"]
    exec_resp = await exec_client.post(f"/api/payments/runs/{run_id}/execute")
    assert exec_resp.status_code == 200, exec_resp.text
    return run_id


async def test_hold_opens_a_visible_exception(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_unscreenable_invoice(mk, org_id, number="PCH-001", amount="500.00")

    async with realdb.client(key=TENANT, role="admin") as admin_client:
        async with realdb.client(key=TENANT, role="ap_manager") as mgr_client:
            await _create_and_execute_run(admin_client, mgr_client, invoice_id)

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == invoice_id))
        ).scalar_one()
        assert payment.status == "pending_compliance"

        exc = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == "payment_compliance_hold",
                )
            )
        ).scalar_one()
        assert exc.status == "open"
        assert exc.severity == "error"
        # Keyed (decisions §358): the hold frame over its one reason, so the
        # queue reads it in the reviewer's language. `failure_reason` keeps
        # its English shape.
        assert payment.failure_reason == "compliance_hold: no screenable vendor on invoice"
        assert exc.description_code == "exception.compliance_hold_findings"
        assert exc.description_params["count"] == 1
        assert [f["code"] for f in exc.description_params["findings"]] == [
            "exception.compliance_no_screenable_vendor"
        ]
        assert exc.description == (
            "Payment held for compliance review (1 reason): no screenable vendor on invoice"
        )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get("/api/exceptions")
        items = resp.json()["items"]
        assert any(
            i["invoice_id"] == str(invoice_id) and i["exception_type"] == "payment_compliance_hold"
            for i in items
        )


async def test_dismiss_fails_the_payment_and_resolves_the_exception(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_unscreenable_invoice(mk, org_id, number="PCH-002", amount="500.00")

    async with realdb.client(key=TENANT, role="admin") as admin_client:
        async with realdb.client(key=TENANT, role="ap_manager") as mgr_client:
            await _create_and_execute_run(admin_client, mgr_client, invoice_id)

        async with mk() as s:
            payment_id = (
                await s.execute(select(Payment.id).where(Payment.invoice_id == invoice_id))
            ).scalar_one()

        dismiss_resp = await admin_client.post(
            f"/api/payments/{payment_id}/compliance/dismiss",
            json={"reason": "vendor confirmed defunct, will not chase"},
        )
    assert dismiss_resp.status_code == 200, dismiss_resp.text
    body = dismiss_resp.json()
    assert body["status"] == "failed"

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == invoice_id))
        ).scalar_one()
        assert payment.failure_reason is not None
        assert "defunct" in payment.failure_reason

        exc = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == "payment_compliance_hold",
                )
            )
        ).scalar_one()
        assert exc.status == "resolved"
        assert exc.resolved_by is not None
        assert exc.resolved_at is not None


async def test_release_re_dispatches_once_a_vendor_is_attached(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_unscreenable_invoice(mk, org_id, number="PCH-003", amount="500.00")

    async with realdb.client(key=TENANT, role="admin") as admin_client:
        async with realdb.client(key=TENANT, role="ap_manager") as mgr_client:
            await _create_and_execute_run(admin_client, mgr_client, invoice_id)

        async with mk() as s:
            payment_id = (
                await s.execute(select(Payment.id).where(Payment.invoice_id == invoice_id))
            ).scalar_one()

        # AP steward's fix: attach a real, clean vendor — the release below
        # should now clear the hold via the SAME compliance gate, not bypass it.
        async with mk() as s:
            vendor = Vendor(name="Unlinked Vendor Co", organization_id=org_id)
            s.add(vendor)
            await s.flush()
            inv = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
            inv.vendor_id = vendor.id
            await s.commit()

        release_resp = await admin_client.post(f"/api/payments/{payment_id}/compliance/release")
    assert release_resp.status_code == 200, release_resp.text
    body = release_resp.json()
    assert body["status"] != "pending_compliance"

    async with mk() as s:
        exc = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == "payment_compliance_hold",
                )
            )
        ).scalar_one()
        assert exc.status == "resolved"
        assert exc.resolution == "released"


async def test_release_that_settles_hands_off_to_the_erp_sync(realdb, monkeypatch):
    """`services/payment_erp_sync` is the ONLY path that flips an invoice
    `payment_scheduled → paid`, and nothing re-invokes it for a payment that is
    already `completed`.

    `/release` calls `_execute_single_payment` directly, and that path can
    settle synchronously (the virtual-card leg always does; so does any adapter
    returning `completed`). It committed the money movement and never
    dispatched the sync, so the invoice stranded at `payment_scheduled`
    forever — under-counting the aging report, the `/dashboard` pipeline, the
    vendor's payment history and the 1099 YTD totals while the payment row
    looked correct. The handoff is asserted directly rather than via the
    detached task, so the wiring is pinned without depending on its timing.
    """
    from app.services import payment_erp_sync

    dispatched = AsyncMock()
    monkeypatch.setattr(payment_erp_sync, "dispatch_payment_sync", dispatched)

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_unscreenable_invoice(mk, org_id, number="PCH-006", amount="500.00")

    async with realdb.client(key=TENANT, role="admin") as admin_client:
        async with realdb.client(key=TENANT, role="ap_manager") as mgr_client:
            run_id = await _create_and_execute_run(admin_client, mgr_client, invoice_id)

        async with mk() as s:
            payment_id = (
                await s.execute(select(Payment.id).where(Payment.invoice_id == invoice_id))
            ).scalar_one()

        # A release that STAYS held moves no money, so it must not dispatch.
        held = await admin_client.post(f"/api/payments/{payment_id}/compliance/release")
        assert held.status_code == 200, held.text
        assert held.json()["status"] == "pending_compliance"
        assert dispatched.await_count == 0

        async with mk() as s:
            vendor = Vendor(name="Sync Handoff Co", organization_id=org_id)
            s.add(vendor)
            await s.flush()
            inv = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
            inv.vendor_id = vendor.id
            await s.commit()

        released = await admin_client.post(f"/api/payments/{payment_id}/compliance/release")

    assert released.status_code == 200, released.text
    assert released.json()["status"] == "completed"
    dispatched.assert_awaited_once_with(uuid.UUID(run_id), org_id)


async def test_release_and_dismiss_409_on_a_non_held_payment(realdb):
    """Both endpoints must refuse a payment that isn't actually held —
    otherwise /release could re-dispatch an already-completed payment, or
    /dismiss could silently fail one still in flight."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = uuid.uuid4()
    payment_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=invoice_id,
                invoice_number="PCH-004",
                vendor_name="Normal Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
            )
        )
        s.add(
            Payment(
                id=payment_id,
                invoice_id=invoice_id,
                amount=Decimal("100.00"),
                method="ach",
                status="completed",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        release_resp = await c.post(f"/api/payments/{payment_id}/compliance/release")
        dismiss_resp = await c.post(
            f"/api/payments/{payment_id}/compliance/dismiss", json={"reason": "n/a"}
        )
    assert release_resp.status_code == 409, release_resp.text
    assert dismiss_resp.status_code == 409, dismiss_resp.text


async def test_clearing_the_hold_writes_the_append_only_audit_row(realdb):
    """Clearing a compliance hold is the sign-off that releases held money, so
    it must leave a row on the invoice's SOX trail — not only on the mutable
    `exceptions` row, which the next decision overwrites.

    This path used to carry its own copy of the resolution bookkeeping and
    wrote no audit row at all; it now goes through
    `services/exception_lifecycle.record_decision`, the same chokepoint the
    human queue and the autonomous agents use.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_unscreenable_invoice(mk, org_id, number="PCH-005", amount="750.00")

    async with realdb.client(key=TENANT, role="admin") as admin_client:
        async with realdb.client(key=TENANT, role="ap_manager") as mgr_client:
            await _create_and_execute_run(admin_client, mgr_client, invoice_id)

        async with mk() as s:
            payment_id = (
                await s.execute(select(Payment.id).where(Payment.invoice_id == invoice_id))
            ).scalar_one()

        dismiss_resp = await admin_client.post(
            f"/api/payments/{payment_id}/compliance/dismiss",
            json={"reason": "sanctions match confirmed by counsel"},
        )
    assert dismiss_resp.status_code == 200, dismiss_resp.text

    async with mk() as s:
        invoice = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        exc = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == "payment_compliance_hold",
                )
            )
        ).scalar_one()

        # Delegating must not have changed what the queue row ends up saying.
        assert exc.status == "resolved"
        assert exc.resolved_by is not None
        assert exc.resolved_at is not None

        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "exception.resolved",
                        AuditLog.entity_id == exc.id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1, "exactly one immutable row per decision"
    row = rows[0]
    # Correlated to the INVOICE so it lands beside invoice.approved on the
    # trail an auditor pulls, not orphaned under the exception's own id.
    assert row.correlation_id == invoice.correlation_id
    assert row.actor_id is not None, "the decision must name who made it"
    assert row.details["exception_type"] == "payment_compliance_hold"
    assert row.details["old_status"] == "open"
    assert row.details["new_status"] == "resolved"
    assert "sanctions match confirmed by counsel" in row.details["resolution"]
    # PII-lean: the generated description can name the vendor; it is not copied.
    assert "Unlinked Vendor Co" not in str(row.details)
