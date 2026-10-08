"""`DELETE /api/invoices/{id}` must cascade through every NO ACTION FK.

`_delete_invoice_cascade` used to clear only six tables (exceptions,
payments, payment_schedules, workflow_instances, invoice_extraction_results,
invoice_line_items). Every other table with a `NO ACTION` FK to invoices
(virtual_cards, agent_decisions, peppol_transmissions, credit_memos,
discount_offers, supplier_chat_threads, vendor_statement_recon_lines) was
missing — deleting an invoice that had one of those rows raised an unhandled
IntegrityError (500) instead of a clean delete. Grandchildren of those
tables (card_reveal_tokens/card_rebates under virtual_cards,
supplier_chat_messages under supplier_chat_threads) needed clearing first.
corporate_card_transactions is a separate case: it's an independently
imported bank-statement feed reconciled TO a virtual card, not owned by the
invoice, so it's unlinked (FK set NULL) rather than deleted.

Found live by exploratory persona-driven testing (approver persona).
Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.credit_memo import CreditMemo
from app.models.expense import CorporateCardTransaction
from app.models.invoice import Invoice, InvoiceStatus
from app.models.supplier_chat import (
    ChatAuthorRole,
    ChatThreadStatus,
    SupplierChatMessage,
    SupplierChatThread,
)
from app.models.vendor import Vendor
from app.models.virtual_card import CardRebate, CardRevealToken, VirtualCard
from app.models.workflow import AuditLog

pytestmark = pytest.mark.asyncio

TENANT = "a"


async def _seed_invoice(mk, org_id, *, number: str) -> str:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Delete Cascade Vendor",
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


async def test_delete_invoice_with_virtual_card_succeeds(realdb):
    """The exact live repro: an approved invoice with an auto-issued
    virtual card (plus its own reveal token + rebate) must delete cleanly,
    not 500."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELCASC-CARD")

    async with mk() as s:
        card = VirtualCard(
            organization_id=org_id,
            invoice_id=uuid.UUID(invoice_id),
            card_provider="mock",
            provider_card_id=f"card_{uuid.uuid4().hex[:8]}",
            amount_limit=Decimal("500.00"),
            status="charged",
        )
        s.add(card)
        await s.flush()
        s.add(
            CardRevealToken(
                token_hash="a" * 64,
                card_id=card.id,
                organization_id=org_id,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        s.add(
            CardRebate(
                organization_id=org_id,
                virtual_card_id=card.id,
                amount=Decimal("7.50"),
                rate=Decimal("0.0150"),
            )
        )
        s.add(
            CorporateCardTransaction(
                organization_id=org_id,
                virtual_card_id=card.id,
                txn_date=date.today(),
                amount=Decimal("500.00"),
            )
        )
        await s.commit()
        card_id = card.id

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204, resp.text

    async with mk() as s:
        assert (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).first() is None
        assert (
            await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))
        ).first() is None
        assert (
            await s.execute(select(CardRevealToken).where(CardRevealToken.card_id == card_id))
        ).first() is None
        assert (
            await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id))
        ).first() is None
        # Unlinked, not deleted — the bank-statement feed row survives.
        txn = (
            await s.execute(
                select(CorporateCardTransaction).where(
                    CorporateCardTransaction.organization_id == org_id
                )
            )
        ).scalar_one()
        assert txn.virtual_card_id is None


async def test_delete_invoice_with_applied_credit_memo_succeeds(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELCASC-MEMO")

    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Delete Cascade Vendor")
        s.add(vendor)
        await s.flush()
        s.add(
            CreditMemo(
                memo_number="CM-DELCASC",
                vendor_id=vendor.id,
                invoice_id=uuid.UUID(invoice_id),
                amount=Decimal("50.00"),
                status="applied",
                organization_id=org_id,
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204, resp.text

    async with mk() as s:
        assert (
            await s.execute(select(CreditMemo).where(CreditMemo.memo_number == "CM-DELCASC"))
        ).first() is None


async def test_delete_invoice_with_supplier_chat_thread_succeeds(realdb):
    """Grandchild-of-grandchild ordering: messages must clear before their
    thread, and the thread before the invoice."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELCASC-CHAT")

    async with mk() as s:
        thread = SupplierChatThread(
            organization_id=org_id,
            invoice_id=uuid.UUID(invoice_id),
            status=ChatThreadStatus.open,
        )
        s.add(thread)
        await s.flush()
        s.add(
            SupplierChatMessage(
                thread_id=thread.id,
                author_role=ChatAuthorRole.ap_team,
                author_user_id=None,
                body="test message",
            )
        )
        await s.commit()
        thread_id = thread.id

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204, resp.text

    async with mk() as s:
        assert (
            await s.execute(select(SupplierChatThread).where(SupplierChatThread.id == thread_id))
        ).first() is None
        assert (
            await s.execute(
                select(SupplierChatMessage).where(SupplierChatMessage.thread_id == thread_id)
            )
        ).first() is None


# ---------------------------------------------------------------------------
# A hard delete is a tenant mutation, and it was the one leaving no record.
#
# `_delete_invoice_cascade` removes the invoice plus its exceptions, payments,
# payment schedules, workflow instances/steps, extraction results, line items,
# agent decisions, PEPPOL transmissions, credit memos, discount offers, chat
# threads and virtual cards. It deliberately does NOT touch `audit_log`, which
# is what lets a terminal `invoice.deleted` row survive the delete and keeps the
# invoice's whole prior trail readable afterwards.
#
# The audit sweep in `test_audit_append_only.py` did not catch this: it asks
# whether the string `dispatch_audit` appears anywhere in the MODULE, and
# `api/invoices.py` has 21 tenant-mutating routes, so one auditing handler
# exempted the other twenty.
# ---------------------------------------------------------------------------


async def _deletion_rows(mk, invoice_id: str) -> list[AuditLog]:
    async with mk() as s:
        rows = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == uuid.UUID(invoice_id),
                    AuditLog.action == "invoice.deleted",
                )
            )
        ).scalars()
        return list(rows)


async def test_deleting_an_invoice_writes_a_surviving_audit_row(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELAUDIT-1")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204

    # The invoice is gone...
    async with mk() as s:
        assert (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one_or_none() is None

    # ...and the record of who removed it is not.
    rows = await _deletion_rows(mk, invoice_id)
    assert len(rows) == 1, "a hard delete must leave exactly one terminal audit row"
    row = rows[0]
    assert row.actor_id is not None
    assert row.details["invoice_number"] == "DELAUDIT-1"
    assert row.details["bulk"] is False
    # PII-free: the business reference and the status, nothing about the payee.
    assert "vendor_name" not in row.details
    assert "remit_to_address" not in row.details


async def test_bulk_delete_audits_each_invoice_it_actually_deletes(realdb):
    """And only those — a skipped (immutable-status) invoice must not be
    recorded as deleted, or the trail asserts a deletion that never happened."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    deletable = await _seed_invoice(mk, org_id, number="DELAUDIT-BULK-OK")

    async with mk() as s:
        blocked = Invoice(
            organization_id=org_id,
            invoice_number="DELAUDIT-BULK-SKIP",
            vendor_name="Delete Cascade Vendor",
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.paid,  # in IMMUTABLE_STATUSES
        )
        s.add(blocked)
        await s.commit()
        await s.refresh(blocked)
        blocked_id = str(blocked.id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [deletable, blocked_id]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 1
    assert body["skipped"] == [blocked_id]

    assert len(await _deletion_rows(mk, deletable)) == 1
    assert await _deletion_rows(mk, blocked_id) == []


# ---------------------------------------------------------------------------
# An approved invoice is not immutable, but it can already be in a payment run
# — exported to the bank, even. Deleting it cascaded the live payment away
# while the bank still paid it, so the ledger stopped matching the money that
# moved. Both delete doors now refuse it; the way out is cancel / void.
# ---------------------------------------------------------------------------


async def _with_pending_payment(mk, invoice_id: str) -> None:
    from app.models.payment import Payment

    async with mk() as s:
        s.add(
            Payment(
                invoice_id=uuid.UUID(invoice_id),
                amount=Decimal("500.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()


async def _still_there(mk, invoice_id: str) -> bool:
    async with mk() as s:
        row = await s.execute(select(Invoice.id).where(Invoice.id == uuid.UUID(invoice_id)))
        return row.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_delete_refuses_an_invoice_with_a_live_payment(realdb):
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, realdb.info(TENANT).org_id, number="DEL-LIVE-PAY-1")
    await _with_pending_payment(mk, inv_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 409, resp.text
    assert "payment run" in resp.json()["detail"]
    assert await _still_there(mk, inv_id)


@pytest.mark.asyncio
async def test_bulk_delete_skips_an_invoice_with_a_live_payment(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    paying = await _seed_invoice(mk, org_id, number="DEL-LIVE-PAY-2")
    free = await _seed_invoice(mk, org_id, number="DEL-LIVE-PAY-3")
    await _with_pending_payment(mk, paying)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [paying, free]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "skipped": [paying]}
    assert await _still_there(mk, paying)
    assert not await _still_there(mk, free)
