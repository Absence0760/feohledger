"""Supplier-portal self-service — PO flip, remittance, company self-update.

DB-backed (the `realdb` fixture) because the security-critical behaviour here
is data-layer: a flipped invoice carries the right vendor_id + Decimal amount,
a bank change stages WITHOUT mutating the vendor, and AP approval applies it.
Source-level mocks can't prove the staging table actually leaves the vendor
untouched — only a real round-trip can.

Each test uses ONE tenant sessionmaker (`realdb.sessionmaker`) threaded through
every seed/read so a single engine is reused per test — matching the stable
pattern in `test_credit_memos.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import create_vendor_access_token
from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.procurement import POLineItem, PurchaseOrder
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog

TENANT = "a"


async def _seed_vendor_and_user(mk, org_id, *, name="Acme Supply") -> tuple[uuid.UUID, uuid.UUID]:
    vendor_id = uuid.uuid4()
    vu_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name=name,
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


def _portal_client(realdb, vendor_user_id: uuid.UUID, vendor_id: uuid.UUID):
    """A realdb ASGI client carrying a vendor JWT instead of an employee one."""
    token = create_vendor_access_token(vendor_user_id, vendor_id)
    client = realdb.client(key=TENANT, role=None)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# PO flip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_po_flip_creates_invoice_from_po(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    po_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PurchaseOrder(
                id=po_id,
                po_number="PO-FLIP-1",
                vendor_id=vendor_id,
                total=Decimal("4250.75"),
                status="open",
                organization_id=org_id,
            )
        )
        s.add(
            POLineItem(
                po_id=po_id,
                description="Widgets",
                quantity=Decimal("10"),
                unit_price=Decimal("425.075"),
                total=Decimal("4250.75"),
            )
        )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/purchase-orders/{po_id}/flip")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] in (InvoiceStatus.new.value, "pending")

    async with mk() as s:
        inv = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(body["id"])))
        ).scalar_one()
        assert inv.vendor_id == vendor_id
        assert inv.po_number == "PO-FLIP-1"
        assert inv.amount == Decimal("4250.75")
        assert isinstance(inv.amount, Decimal)
        lines = (
            (await s.execute(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == inv.id)))
            .scalars()
            .all()
        )
        assert lines  # PO line copied across
        assert lines[0].description == "Widgets"
        actions = {r.action for r in (await s.execute(select(AuditLog))).scalars().all()}
    assert "invoice.created_from_po" in actions


@pytest.mark.asyncio
async def test_portal_pos_carry_their_own_currency_and_the_flip_inherits_it(realdb):
    """A supplier sees each PO in the currency it was raised in, and an
    invoice flipped from it is booked in that currency.

    `PortalPOListItem.currency` defaulted to "USD" and the handler never set
    it, so every PO a supplier saw read as dollars; the flip hardcoded "USD"
    on the invoice too — a EUR order billed as a USD invoice of the same
    figure (decisions §197). A PO that records no currency is served `null`
    (the portal renders it bare), not a borrowed code.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    eur_id, bare_id = uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        for po_id, number, currency in ((eur_id, "PO-P-EUR", "EUR"), (bare_id, "PO-P-NONE", None)):
            s.add(
                PurchaseOrder(
                    id=po_id,
                    po_number=number,
                    vendor_id=vendor_id,
                    total=Decimal("300.00"),
                    currency=currency,
                    status="open",
                    organization_id=org_id,
                )
            )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        listed = (await client.get("/api/portal/purchase-orders")).json()["items"]
        eur_detail = (await client.get(f"/api/portal/purchase-orders/{eur_id}")).json()
        bare_detail = (await client.get(f"/api/portal/purchase-orders/{bare_id}")).json()
        flipped = await client.post(f"/api/portal/purchase-orders/{eur_id}/flip")
    assert flipped.status_code == 202, flipped.text

    assert {row["po_number"]: row["currency"] for row in listed} == {
        "PO-P-EUR": "EUR",
        "PO-P-NONE": None,
    }
    assert eur_detail["currency"] == "EUR"
    assert bare_detail["currency"] is None
    async with mk() as s:
        inv = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(flipped.json()["id"])))
        ).scalar_one()
    assert inv.currency == "EUR"


@pytest.mark.asyncio
async def test_po_flip_is_idempotent(realdb):
    """Two flips of the same PO from the same vendor return the same invoice
    — a double-click can't mint two invoices that each seed the payment path."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    po_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PurchaseOrder(
                id=po_id,
                po_number="PO-FLIP-DUP",
                vendor_id=vendor_id,
                total=Decimal("100.00"),
                status="open",
                organization_id=org_id,
            )
        )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        first = await client.post(f"/api/portal/purchase-orders/{po_id}/flip")
        second = await client.post(f"/api/portal/purchase-orders/{po_id}/flip")
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]

    async with mk() as s:
        count = (
            await s.execute(
                select(func.count()).select_from(Invoice).where(Invoice.po_number == "PO-FLIP-DUP")
            )
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_po_flip_marker_has_db_level_unique_guard(realdb):
    """The durable backstop: the partial unique index on the `po-flip:<po_id>`
    marker (migration 0024 / Invoice.__table_args__) rejects a second invoice
    for the same PO at the DB layer, so a race that slips past the app-level
    existing-invoice check still cannot persist two payment-seeding invoices."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, _ = await _seed_vendor_and_user(mk, org_id)
    po_id = uuid.uuid4()
    marker = f"po-flip:{po_id}"

    async with mk() as s:
        s.add(
            Invoice(
                invoice_number="",
                vendor_name="Acme",
                vendor_id=vendor_id,
                amount=Decimal("10.00"),
                status=InvoiceStatus.new,
                reference_number=marker,
                organization_id=org_id,
            )
        )
        await s.commit()

    # A second invoice carrying the identical flip marker must be rejected by
    # the partial unique index — independent of any application-level check.
    with pytest.raises(IntegrityError):
        async with mk() as s:
            s.add(
                Invoice(
                    invoice_number="",
                    vendor_name="Acme",
                    vendor_id=vendor_id,
                    amount=Decimal("10.00"),
                    status=InvoiceStatus.new,
                    reference_number=marker,
                    organization_id=org_id,
                )
            )
            await s.commit()

    # An ordinary invoice (reference_number not a flip marker) is unaffected —
    # the predicate keeps the constraint scoped to flips only.
    async with mk() as s:
        s.add(
            Invoice(
                invoice_number="",
                vendor_name="Acme",
                vendor_id=vendor_id,
                amount=Decimal("5.00"),
                status=InvoiceStatus.new,
                reference_number="REF-NORMAL-001",
                organization_id=org_id,
            )
        )
        s.add(
            Invoice(
                invoice_number="",
                vendor_name="Acme",
                vendor_id=vendor_id,
                amount=Decimal("6.00"),
                status=InvoiceStatus.new,
                reference_number="REF-NORMAL-001",
                organization_id=org_id,
            )
        )
        await s.commit()  # no raise — duplicate non-flip references are allowed


@pytest.mark.asyncio
async def test_po_flip_cross_vendor_404(realdb):
    """A PO owned by a different vendor is invisible — 404, never flipped."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    other_vendor_id, _ = await _seed_vendor_and_user(mk, org_id, name="Other Co")
    po_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PurchaseOrder(
                id=po_id,
                po_number="PO-OTHER",
                vendor_id=other_vendor_id,
                total=Decimal("999.00"),
                status="open",
                organization_id=org_id,
            )
        )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(f"/api/portal/purchase-orders/{po_id}/flip")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_po_flip_recovery_path_does_not_leak_foreign_invoice(realdb):
    """IntegrityError-recovery isolation: if the `po-flip:<po_id>` marker slot is
    already occupied by ANOTHER vendor's invoice, the losing flush's recovery
    query must be vendor-scoped — it must NOT hand our vendor the foreign
    invoice's id / correlation_id / status.

    We seed a foreign-vendor invoice carrying our PO's flip marker, then flip the
    PO as our vendor. The insert collides on the partial unique marker index →
    IntegrityError → the recovery branch runs. With the vendor-scoped recovery
    query, it finds nothing for our vendor and re-raises rather than returning a
    `PortalFlipResponse` built from the foreign invoice. (An unscoped recovery —
    the bug — would return the other supplier's non-public identifiers.)
    """
    from types import SimpleNamespace

    from app.api.portal import flip_purchase_order

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id, name="Mine Co")
    foreign_vendor_id, _ = await _seed_vendor_and_user(mk, org_id, name="Foreign Co")

    po_id = uuid.uuid4()
    foreign_invoice_id = uuid.uuid4()
    marker = f"po-flip:{po_id}"
    async with mk() as s:
        # Our vendor owns the PO (so the fast-path PO lookup passes)…
        s.add(
            PurchaseOrder(
                id=po_id,
                po_number="PO-RACE",
                vendor_id=vendor_id,
                total=Decimal("100.00"),
                status="open",
                organization_id=org_id,
            )
        )
        # …but the flip marker slot is already taken by a FOREIGN vendor's invoice.
        s.add(
            Invoice(
                id=foreign_invoice_id,
                invoice_number="",
                vendor_name="Foreign Co",
                vendor_id=foreign_vendor_id,
                amount=Decimal("100.00"),
                status=InvoiceStatus.new,
                reference_number=marker,
                organization_id=org_id,
            )
        )
        await s.commit()

    vu = SimpleNamespace(id=vu_id, vendor_id=vendor_id)
    # The scoped recovery finds no invoice for OUR vendor → the original
    # IntegrityError propagates. The foreign invoice's id is never disclosed.
    async with mk() as s:
        with pytest.raises(IntegrityError):
            await flip_purchase_order(po_id=po_id, db=s, vu=vu, idempotency_key=None)

    # Belt-and-suspenders: no new invoice was minted for our vendor, and the
    # foreign row is untouched.
    async with mk() as s:
        mine = (
            (await s.execute(select(Invoice).where(Invoice.vendor_id == vendor_id))).scalars().all()
        )
        assert mine == []


# ---------------------------------------------------------------------------
# Remittance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remittance_download_for_own_payment(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    inv_id = uuid.uuid4()
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number="INV-REM-1",
                vendor_name="Acme Supply",
                vendor_id=vendor_id,
                amount=Decimal("321.00"),
                currency="USD",
                status=InvoiceStatus.paid,
                organization_id=org_id,
            )
        )
        run = PaymentRun(organization_id=org_id, status="completed", total_amount=Decimal("321.00"))
        s.add(run)
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                payment_run_id=run.id,
                invoice_id=inv_id,
                amount=Decimal("321.00"),
                method="ach",
                status="completed",
                reference="REM-REF-1",
                completed_at=datetime.now(UTC),
            )
        )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get(f"/api/portal/payments/{pay_id}/remittance")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_remittance_filename_survives_quote_in_reference(realdb):
    """Issue #188: a payment `reference` (processor-supplied, not a validated
    identifier) carrying a `"` must not break the Content-Disposition
    header's quoted-string syntax."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    inv_id = uuid.uuid4()
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number="INV-REM-2",
                vendor_name="Acme Supply",
                vendor_id=vendor_id,
                amount=Decimal("321.00"),
                currency="USD",
                status=InvoiceStatus.paid,
                organization_id=org_id,
            )
        )
        run = PaymentRun(organization_id=org_id, status="completed", total_amount=Decimal("321.00"))
        s.add(run)
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                payment_run_id=run.id,
                invoice_id=inv_id,
                amount=Decimal("321.00"),
                method="ach",
                status="completed",
                reference='REM-REF"2"',
                completed_at=datetime.now(UTC),
            )
        )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get(f"/api/portal/payments/{pay_id}/remittance")
    assert resp.status_code == 200, resp.text
    disposition = resp.headers["content-disposition"]
    assert disposition.count('"') == 2
    assert "filename*=UTF-8''" in disposition


@pytest.mark.asyncio
async def test_remittance_foreign_payment_404(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    other_vendor_id, _ = await _seed_vendor_and_user(mk, org_id, name="Other Co")
    inv_id = uuid.uuid4()
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number="INV-OTHER",
                vendor_name="Other Co",
                vendor_id=other_vendor_id,
                amount=Decimal("50.00"),
                currency="USD",
                status=InvoiceStatus.paid,
                organization_id=org_id,
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("50.00"),
                method="ach",
                status="completed",
                reference="FOREIGN",
            )
        )
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get(f"/api/portal/payments/{pay_id}/remittance")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Company self-service + change-request staging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_company_patch_applies_contact_fields_live(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.patch(
            "/api/portal/company",
            json={"phone": "555-0100", "address": "1 New St"},
        )
    assert resp.status_code == 200, resp.text
    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert v.phone == "555-0100"
        assert v.address == "1 New St"


@pytest.mark.asyncio
async def test_bank_change_stages_and_does_not_mutate_vendor(realdb):
    """The fraud control: a bank change creates a PENDING row and leaves
    `Vendor.bank_details` untouched."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            "/api/portal/company/bank-change",
            json={"bank_details": {"account_number": "12345678", "bank_name": "New Bank"}},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["change_type"] == "bank_details"
    assert body["status"] == "pending"

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert v.bank_details is None  # NOT applied
        req = (
            await s.execute(
                select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
            )
        ).scalar_one()
        assert req.status == "pending"
        assert req.proposed_value["bank_details"]["account_number"] == "12345678"


@pytest.mark.asyncio
async def test_bank_change_rejects_a_malformed_routing_number(realdb):
    """Same structural gate as the AP-initiated staging path — a vendor
    self-service submission is exactly as unvalidated otherwise.

    400, not 422: the check moved out of the Pydantic field validator, whose
    422 body echoed the submitted `bank_details` (account number included)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            "/api/portal/company/bank-change",
            json={"bank_details": {"routing_number": "021000020", "account_number": "12345678"}},
        )
    assert resp.status_code == 400, resp.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_bank_change_rejects_a_malformed_sort_code(realdb):
    """UK equivalent of the routing-number check — a vendor self-service
    submission with a bad sort code is rejected before staging, same as a
    bad ABA routing number."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            "/api/portal/company/bank-change",
            json={"bank_details": {"sort_code": "1234567", "account_number": "12345678"}},
        )
    assert resp.status_code == 400, resp.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_bank_change_accepts_a_valid_sort_code(realdb):
    """A well-formed bare-digit sort code stages cleanly."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            "/api/portal/company/bank-change",
            json={"bank_details": {"sort_code": "200000", "account_number": "12345678"}},
        )
    assert resp.status_code == 202, resp.text


@pytest.mark.asyncio
async def test_tax_id_change_stages_and_does_not_mutate_vendor(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post("/api/portal/company/tax-id-change", json={"tax_id": "98-7654321"})
    assert resp.status_code == 202, resp.text

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert v.tax_id is None  # NOT applied


@pytest.mark.asyncio
async def test_duplicate_pending_bank_change_409(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with _portal_client(realdb, vu_id, vendor_id) as client:
        first = await client.post(
            "/api/portal/company/bank-change",
            json={"bank_details": {"account_number": "111"}},
        )
        second = await client.post(
            "/api/portal/company/bank-change",
            json={"bank_details": {"account_number": "222"}},
        )
    assert first.status_code == 202
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_company_get_masks_bank_and_tax(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        v.tax_id = "12-3456789"
        v.bank_details = {"account_last4": "6789"}
        await s.commit()

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get("/api/portal/company")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tax_id_last4"] == "6789"
    assert body["has_bank_details"] is True
    # The full tax id must never appear in the payload.
    assert "12-3456789" not in resp.text


@pytest.mark.asyncio
async def test_portal_bank_change_names_the_field_without_echoing_banking_data(realdb):
    """The 4xx must name the offending field and nothing else.

    This ran as a Pydantic `field_validator`, and FastAPI renders a
    ValidationError as a 422 whose body echoes the rejected `input` — here the
    whole `bank_details` dict, account number included. So "your routing number
    has a typo" answered with banking data. Invariant: PII/banking data stays
    out of logs and error bodies."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id, name="Routing Echo Co")

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            "/api/portal/company/bank-change",
            json={
                "bank_details": {
                    "account_number": "987654321098",
                    "routing_number": "123456789",  # fails the ABA checksum
                    "bank_name": "Test Bank",
                }
            },
        )
    assert resp.status_code == 400, resp.text
    body = resp.text
    assert "routing_number" in body
    assert "987654321098" not in body
    assert "123456789" not in body


@pytest.mark.asyncio
async def test_portal_bank_change_validates_the_wire_routing_number_too(realdb):
    """The portal path was narrower than the AP one: it checked only
    `routing_number`, so a malformed wire ABA reached staging unvalidated and
    failed only at approve. Both paths now share one helper."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id, name="Wire Echo Co")

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.post(
            "/api/portal/company/bank-change",
            json={
                "bank_details": {
                    "account_number": "987654321098",
                    "wire_routing_number": "123456789",  # fails the ABA checksum
                }
            },
        )
    assert resp.status_code == 400, resp.text
    assert "wire_routing_number" in resp.text
    assert "987654321098" not in resp.text
