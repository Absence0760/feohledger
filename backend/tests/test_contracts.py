"""Real-DB coverage for the contracts router.

Covers ``backend/app/api/contracts.py`` end-to-end against the live test
tenants: repository CRUD, document upload, the lifecycle transitions
(activate / terminate / cancel / renew) with their 409 guards, the
spend-to-contract rollup (``services.contract_spend``), RBAC, tenant
isolation, audit rows, and exact ``Numeric`` money round-trips.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.models.contract import Contract
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.models.workflow import AuditLog


async def _add_vendor(mk, org_id, name="Globex Industrial") -> str:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name)
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return str(v.id)


async def _create_contract(c, vendor_id, **overrides) -> dict:
    body = {
        "contract_number": "MSA-2026-001",
        "title": "Master Services Agreement",
        "contract_type": "msa",
        "vendor_id": vendor_id,
        "currency": "USD",
        "total_value": "120000.00",
        "spend_limit": "100000.00",
        "end_date": "2027-01-01",
        "renewal_notice_days": 45,
    }
    body.update(overrides)
    resp = await c.post("/api/contracts", json=body)
    return resp


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_contract_with_line_items(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await _create_contract(
            c,
            vendor_id,
            line_items=[
                {
                    "description": "Tier 1 support",
                    "quantity": "12",
                    "unit_price": "1000.00",
                    "total": "12000.00",
                    "gl_account": "6000",
                },
            ],
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["contract_number"] == "MSA-2026-001"
    assert body["status"] == "draft"
    assert body["contract_type"] == "msa"
    assert body["vendor_name"] == "Globex Industrial"
    assert len(body["line_items"]) == 1
    assert body["line_items"][0]["line_number"] == 1

    # Exact Decimal round-trip through Numeric(15, 2).
    async with mk() as s:
        contract = (await s.execute(select(Contract))).scalar_one()
        assert contract.total_value == Decimal("120000.00")
        assert contract.spend_limit == Decimal("100000.00")
        assert contract.organization_id == org_id
        # Audit row written on create.
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "contract")))
            .scalars()
            .all()
        )
        assert "contract.created" in actions


async def test_create_contract_unknown_vendor_404(realdb):
    import uuid

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await _create_contract(c, str(uuid.uuid4()))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# list + filters
# ---------------------------------------------------------------------------


async def test_list_and_filter(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        await _create_contract(c, vendor_id, contract_number="C-AAA", contract_type="msa")
        await _create_contract(c, vendor_id, contract_number="C-BBB", contract_type="lease")

    # A clerk can read the repository even though it can't create.
    async with realdb.client(key="a", role="ap_clerk") as c:
        all_resp = await c.get("/api/contracts")
        assert all_resp.status_code == 200
        assert all_resp.json()["total"] >= 2

        typed = await c.get("/api/contracts?contract_type=lease")
        assert all(i["contract_type"] == "lease" for i in typed.json()["items"])

        searched = await c.get("/api/contracts?search=C-AAA")
        nums = [i["contract_number"] for i in searched.json()["items"]]
        assert "C-AAA" in nums and "C-BBB" not in nums


# ---------------------------------------------------------------------------
# spend rollup
# ---------------------------------------------------------------------------


async def test_spend_summary_over_limit(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        created = (await _create_contract(c, vendor_id, spend_limit="1000.00")).json()
        contract_id = created["id"]

    # Link two invoices directly (the link endpoint lands in a later slice).
    import uuid

    async with mk() as s:
        for amt in (Decimal("600.00"), Decimal("700.00")):
            s.add(
                Invoice(
                    organization_id=org_id,
                    invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                    vendor_name="Globex Industrial",
                    amount=amt,
                    status=InvoiceStatus.approved,
                    vendor_id=uuid.UUID(vendor_id),
                    contract_id=uuid.UUID(contract_id),
                )
            )
        # A rejected invoice must NOT count toward spend.
        s.add(
            Invoice(
                organization_id=org_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Globex Industrial",
                amount=Decimal("9999.00"),
                status=InvoiceStatus.rejected,
                vendor_id=uuid.UUID(vendor_id),
                contract_id=uuid.UUID(contract_id),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (await c.get(f"/api/contracts/{contract_id}")).json()
    assert detail["spend"]["invoiced_total"] == 1300.0
    assert detail["spend"]["invoice_count"] == 2
    assert detail["spend"]["remaining"] == -300.0
    assert detail["spend"]["over_limit"] is True


async def test_spend_summary_excludes_foreign_currency_invoices(realdb):
    """A USD contract never sums a linked invoice denominated in another
    currency into its spend rollup — the legs don't convert."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        created = (await _create_contract(c, vendor_id, spend_limit="1000.00")).json()
        contract_id = created["id"]

    async with mk() as s:
        s.add(
            Invoice(
                organization_id=org_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Globex Industrial",
                amount=Decimal("300.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                vendor_id=uuid.UUID(vendor_id),
                contract_id=uuid.UUID(contract_id),
            )
        )
        s.add(
            Invoice(
                organization_id=org_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Globex Industrial",
                amount=Decimal("900.00"),
                currency="EUR",
                status=InvoiceStatus.approved,
                vendor_id=uuid.UUID(vendor_id),
                contract_id=uuid.UUID(contract_id),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (await c.get(f"/api/contracts/{contract_id}")).json()
    assert detail["spend"]["invoiced_total"] == 300.0  # EUR row excluded, not added as 900
    assert detail["spend"]["invoice_count"] == 1
    assert detail["spend"]["over_limit"] is False


async def test_compliance_over_limit_excludes_foreign_currency(realdb):
    """The not-to-exceed compliance check never raises a false ``error``
    finding from a foreign-currency invoice inflating the cumulative sum."""
    from app.services.contract_compliance import evaluate_contract_compliance

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        created = (
            await _create_contract(c, vendor_id, spend_limit="1000.00", not_to_exceed=True)
        ).json()
        contract_id = uuid.UUID(created["id"])

    async with mk() as s:
        # A prior EUR invoice that must not count toward the USD limit.
        s.add(
            Invoice(
                organization_id=org_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Globex Industrial",
                amount=Decimal("900.00"),
                currency="EUR",
                status=InvoiceStatus.approved,
                vendor_id=uuid.UUID(vendor_id),
                contract_id=contract_id,
            )
        )
        # The invoice under evaluation: $300 USD, well under the $1000 limit.
        new_invoice = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
            vendor_name="Globex Industrial",
            amount=Decimal("300.00"),
            currency="USD",
            status=InvoiceStatus.new,
            vendor_id=uuid.UUID(vendor_id),
            contract_id=contract_id,
        )
        s.add(new_invoice)
        await s.commit()
        await s.refresh(new_invoice)

    async with mk() as s:
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == new_invoice.id))
        ).scalar_one()
        findings = await evaluate_contract_compliance(s, invoice)
    assert not any("exceeds contract" in f["message"] for f in findings)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_contract(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id)).json()["id"]
        resp = await c.patch(
            f"/api/contracts/{contract_id}",
            json={"title": "Renamed MSA", "spend_limit": "150000.00"},
        )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed MSA"
    assert resp.json()["spend_limit"] == 150000.0

    async with mk() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.action == "contract.updated")))
            .scalars()
            .all()
        )
        assert len(actions) >= 1


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


async def test_lifecycle_activate_terminate(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id)).json()["id"]

        activated = await c.post(f"/api/contracts/{contract_id}/activate")
        assert activated.status_code == 200
        assert activated.json()["status"] == "active"

        # Can't activate an already-active contract.
        again = await c.post(f"/api/contracts/{contract_id}/activate")
        assert again.status_code == 409

        terminated = await c.post(f"/api/contracts/{contract_id}/terminate")
        assert terminated.status_code == 200
        assert terminated.json()["status"] == "terminated"


async def test_renew_extends_and_clears_alert(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id, end_date="2027-01-01")).json()["id"]
        await c.post(f"/api/contracts/{contract_id}/activate")

        # Renewing to an earlier date is rejected.
        bad = await c.post(f"/api/contracts/{contract_id}/renew", json={"end_date": "2026-06-01"})
        assert bad.status_code == 400

        renewed = await c.post(
            f"/api/contracts/{contract_id}/renew",
            json={"end_date": "2028-01-01", "spend_limit": "200000.00"},
        )
    assert renewed.status_code == 200
    assert renewed.json()["end_date"] == "2028-01-01"
    assert renewed.json()["status"] == "active"
    assert renewed.json()["renewal_alert_sent_at"] is None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_guarded_by_status(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id)).json()["id"]
        await c.post(f"/api/contracts/{contract_id}/activate")

        # Active contract cannot be deleted.
        blocked = await c.delete(f"/api/contracts/{contract_id}")
        assert blocked.status_code == 409

        await c.post(f"/api/contracts/{contract_id}/cancel")
        ok = await c.delete(f"/api/contracts/{contract_id}")
        assert ok.status_code == 204


# ---------------------------------------------------------------------------
# RBAC + tenant isolation
# ---------------------------------------------------------------------------


async def test_clerk_cannot_create(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await _create_contract(c, vendor_id)
    assert resp.status_code == 403


async def test_tenant_isolation(realdb):
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    vendor_a = await _add_vendor(mk_a, org_a)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_a, contract_number="ISO-A")).json()["id"]

    # Tenant B must not see tenant A's contract.
    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get(f"/api/contracts/{contract_id}")
        assert resp.status_code == 404
        listing = await c.get("/api/contracts?search=ISO-A")
        assert listing.json()["total"] == 0


# ---------------------------------------------------------------------------
# document upload
# ---------------------------------------------------------------------------


async def test_upload_document(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id)).json()["id"]
        resp = await c.post(
            f"/api/contracts/{contract_id}/upload",
            files={"file": ("contract.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["file_key"].startswith(f"{org_id}/contracts/{contract_id}/")
    assert body["file_url"].startswith("/api/contracts/file/")

    async with mk() as s:
        actions = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "contract.document_uploaded")
            )
        ).scalar()
        assert actions >= 1


# ---------------------------------------------------------------------------
# spend-to-contract linking (invoices router)
# ---------------------------------------------------------------------------


async def _add_invoice(mk, org_id, vendor_id, *, amount="500.00") -> str:
    import uuid

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
            vendor_name="Globex Industrial",
            amount=Decimal(amount),
            status=InvoiceStatus.approved,
            vendor_id=uuid.UUID(vendor_id),
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


async def test_link_and_unlink_contract(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id, spend_limit="1000.00")).json()["id"]
        invoice_id = await _add_invoice(mk, org_id, vendor_id, amount="400.00")

        linked = await c.post(
            f"/api/invoices/{invoice_id}/link-contract", json={"contract_id": contract_id}
        )
        assert linked.status_code == 200, linked.text
        assert linked.json()["contract_id"] == contract_id

        # Spend rollup now reflects the linked invoice.
        detail = (await c.get(f"/api/contracts/{contract_id}")).json()
        assert detail["spend"]["invoiced_total"] == 400.0
        assert detail["spend"]["invoice_count"] == 1

        unlinked = await c.post(f"/api/invoices/{invoice_id}/unlink-contract")
        assert unlinked.status_code == 200
        assert unlinked.json()["contract_id"] is None

        detail2 = (await c.get(f"/api/contracts/{contract_id}")).json()
        assert detail2["spend"]["invoice_count"] == 0

    async with mk() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(
                        AuditLog.action.in_(
                            ["invoice.contract_linked", "invoice.contract_unlinked"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "invoice.contract_linked" in actions
        assert "invoice.contract_unlinked" in actions


async def test_compliance_over_not_to_exceed_limit(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (
            await _create_contract(c, vendor_id, spend_limit="1000.00", not_to_exceed=True)
        ).json()["id"]
        invoice_id = await _add_invoice(mk, org_id, vendor_id, amount="1500.00")

        linked = await c.post(
            f"/api/invoices/{invoice_id}/link-contract", json={"contract_id": contract_id}
        )
    assert linked.status_code == 200
    warnings = linked.json()["warnings"] or []
    compliance = [w for w in warnings if w["type"] == "contract_noncompliant"]
    assert compliance, warnings
    assert any(w["severity"] == "error" for w in compliance)  # not_to_exceed → error

    # A contract_noncompliant exception was raised for the queue.
    async with mk() as s:
        from app.models.exception import Exception as APException

        cnt = (
            await s.execute(
                select(func.count())
                .select_from(APException)
                .where(APException.exception_type == "contract_noncompliant")
            )
        ).scalar()
        assert cnt >= 1


async def test_compliance_expired_contract(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        # Contract already expired.
        contract_id = (
            await _create_contract(c, vendor_id, end_date="2020-01-01", spend_limit=None)
        ).json()["id"]
        invoice_id = await _add_invoice(mk, org_id, vendor_id, amount="100.00")

        linked = await c.post(
            f"/api/invoices/{invoice_id}/link-contract", json={"contract_id": contract_id}
        )
    warnings = linked.json()["warnings"] or []
    assert any(
        w["type"] == "contract_noncompliant" and "expired" in w["message"] for w in warnings
    ), warnings


async def test_link_unknown_contract_404(realdb):
    import uuid

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{invoice_id}/link-contract",
            json={"contract_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# contract-based PO creation
# ---------------------------------------------------------------------------


async def test_create_po_from_contract(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (
            await _create_contract(
                c,
                vendor_id,
                line_items=[
                    {
                        "description": "Licenses",
                        "quantity": "10",
                        "unit_price": "100.00",
                        "total": "1000.00",
                    },
                    {
                        "description": "Onboarding",
                        "quantity": "1",
                        "unit_price": "2500.00",
                        "total": "2500.00",
                    },
                ],
            )
        ).json()["id"]
        await c.post(f"/api/contracts/{contract_id}/activate")

        resp = await c.post(f"/api/contracts/{contract_id}/create-po", json={})
    assert resp.status_code == 201, resp.text
    po = resp.json()
    assert po["vendor_id"] == vendor_id
    # Total auto-derived from line-item totals: 1000 + 2500.
    assert po["total"] == 3500.0
    assert len(po["line_items"]) == 2
    assert po["contract_id"] == contract_id

    # PO persisted + audit row written.
    async with mk() as s:
        from app.models.procurement import PurchaseOrder

        stored = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.id == uuid.UUID(po["id"])))
        ).scalar_one()
        assert stored.total == Decimal("3500.00")
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.action == "contract.po_created")
                )
            )
            .scalars()
            .all()
        )
        assert len(actions) >= 1


async def test_create_po_from_contract_is_denominated_in_the_contracts_currency(realdb):
    """The PO's total is the contract's figures, so it carries the contract's code.

    Before `purchase_orders.currency` existed the PO recorded none, and every
    surface labelled a GBP contract's PO with the org's currency (decisions §197).
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id, currency="GBP")).json()["id"]
        await c.post(f"/api/contracts/{contract_id}/activate")
        resp = await c.post(f"/api/contracts/{contract_id}/create-po", json={})
    assert resp.status_code == 201, resp.text
    assert resp.json()["currency"] == "GBP"

    async with mk() as s:
        from app.models.procurement import PurchaseOrder

        stored = (
            await s.execute(
                select(PurchaseOrder).where(PurchaseOrder.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
    assert stored.currency == "GBP"


async def test_renewal_alert_sweep(realdb):
    from datetime import date, timedelta

    from app.models.notification import Notification
    from app.services.contract_renewal import notify_renewals_once

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    today = date.today()

    async with realdb.client(key="a", role="ap_manager") as c:
        # Within the 30-day notice window → should alert.
        due_id = (
            await _create_contract(c, vendor_id, end_date=(today + timedelta(days=10)).isoformat())
        ).json()["id"]
        await c.post(f"/api/contracts/{due_id}/activate")
        # Far outside the window → should NOT alert.
        far_id = (
            await _create_contract(
                c,
                vendor_id,
                contract_number="FAR-001",
                end_date=(today + timedelta(days=900)).isoformat(),
            )
        ).json()["id"]
        await c.post(f"/api/contracts/{far_id}/activate")

    result = await notify_renewals_once(today=today)
    assert result.alerts_sent >= 1

    async with mk() as s:
        due_notes = (
            (
                await s.execute(
                    select(Notification).where(
                        Notification.event_type == "contract_renewal_due",
                        Notification.entity_id == uuid.UUID(due_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(due_notes) >= 1
        assert due_notes[0].entity_type == "contract"

        far_notes = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.entity_id == uuid.UUID(far_id))
            )
        ).scalar()
        assert far_notes == 0

        due = (
            await s.execute(select(Contract).where(Contract.id == uuid.UUID(due_id)))
        ).scalar_one()
        assert due.renewal_alert_sent_at is not None

    # Idempotent: a second sweep sends no new alert for the already-alerted one.
    before = len(due_notes)
    await notify_renewals_once(today=today)
    async with mk() as s:
        after = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.event_type == "contract_renewal_due",
                    Notification.entity_id == uuid.UUID(due_id),
                )
            )
        ).scalar()
    assert after == before


async def test_renewal_alert_not_stamped_when_dispatch_reached_nobody(realdb, monkeypatch):
    """`renewal_alert_sent_at` is the suppress-forever marker for a contract's
    whole remaining term — only `POST /api/contracts/{id}/renew` clears it — so
    it must record a warning that actually went out.

    `notify_event` never raises; with `FEOH_NOTIFICATIONS_ENABLED` off it
    returns having done nothing, and the sweep stamped the marker anyway and
    counted an `alerts_sent`. The AP manager was then never told this contract
    was coming up for renewal, and no failure was recorded anywhere.
    """
    from datetime import date, timedelta

    from app.config import settings as app_settings
    from app.models.notification import Notification
    from app.services.contract_renewal import notify_renewals_once

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    today = date.today()

    async with realdb.client(key="a", role="ap_manager") as c:
        cid = (
            await _create_contract(
                c,
                vendor_id,
                contract_number="MUTED-001",
                end_date=(today + timedelta(days=10)).isoformat(),
            )
        ).json()["id"]
        await c.post(f"/api/contracts/{cid}/activate")

    monkeypatch.setattr(app_settings, "notifications_enabled", False)
    result = await notify_renewals_once(today=today)

    async with mk() as s:
        contract = (
            await s.execute(select(Contract).where(Contract.id == uuid.UUID(cid)))
        ).scalar_one()
        notes = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.entity_id == uuid.UUID(cid))
            )
        ).scalar()

    assert notes == 0  # nothing was actually sent…
    assert contract.renewal_alert_sent_at is None, (
        "the alert marker was stamped for a warning nobody received — the "
        "renewal would never be raised again for this term"
    )
    assert result.alerts_sent == 0


async def test_renewal_sweep_expires_overdue_contracts(realdb):
    """Issue #186 — ``expired`` was never set at runtime. The renewal sweep's
    end-of-term expiry pass must transition an over-term ``active`` contract
    to ``expired`` (audited, idempotent) while leaving a still-current, a
    ``terminated``, and a ``cancelled`` contract untouched."""
    from datetime import date, timedelta

    from app.models.contract import ContractStatus
    from app.services.contract_renewal import notify_renewals_once

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    today = date.today()

    async with realdb.client(key="a", role="ap_manager") as c:
        # Active, end_date in the past -> should expire.
        overdue_id = (
            await _create_contract(
                c,
                vendor_id,
                contract_number="OVERDUE-001",
                end_date=(today - timedelta(days=5)).isoformat(),
            )
        ).json()["id"]
        await c.post(f"/api/contracts/{overdue_id}/activate")

        # Active, end_date still in the future -> must stay active.
        future_id = (
            await _create_contract(
                c,
                vendor_id,
                contract_number="FUTURE-001",
                end_date=(today + timedelta(days=30)).isoformat(),
            )
        ).json()["id"]
        await c.post(f"/api/contracts/{future_id}/activate")

        # Already terminated, end_date in the past -> must stay terminated,
        # never get swept into `expired`.
        terminated_id = (
            await _create_contract(
                c,
                vendor_id,
                contract_number="TERMINATED-001",
                end_date=(today - timedelta(days=5)).isoformat(),
            )
        ).json()["id"]
        await c.post(f"/api/contracts/{terminated_id}/activate")
        await c.post(f"/api/contracts/{terminated_id}/terminate")

        # Already cancelled, end_date in the past -> must stay cancelled.
        cancelled_id = (
            await _create_contract(
                c,
                vendor_id,
                contract_number="CANCELLED-001",
                end_date=(today - timedelta(days=5)).isoformat(),
            )
        ).json()["id"]
        await c.post(f"/api/contracts/{cancelled_id}/cancel")

    result = await notify_renewals_once(today=today)
    assert result.contracts_expired >= 1

    async with mk() as s:
        overdue = (
            await s.execute(select(Contract).where(Contract.id == uuid.UUID(overdue_id)))
        ).scalar_one()
        assert overdue.status == ContractStatus.expired

        future = (
            await s.execute(select(Contract).where(Contract.id == uuid.UUID(future_id)))
        ).scalar_one()
        assert future.status == ContractStatus.active

        terminated = (
            await s.execute(select(Contract).where(Contract.id == uuid.UUID(terminated_id)))
        ).scalar_one()
        assert terminated.status == ContractStatus.terminated

        cancelled = (
            await s.execute(select(Contract).where(Contract.id == uuid.UUID(cancelled_id)))
        ).scalar_one()
        assert cancelled.status == ContractStatus.cancelled

        expired_actions = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "contract.expired",
                        AuditLog.entity_id == uuid.UUID(overdue_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(expired_actions) == 1

    # Idempotent: a second sweep on an already-expired contract is a no-op —
    # no re-expiry, no second audit row.
    result2 = await notify_renewals_once(today=today)
    assert result2.contracts_expired == 0

    async with mk() as s:
        expired_actions_after = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "contract.expired",
                        AuditLog.entity_id == uuid.UUID(overdue_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(expired_actions_after) == 1


async def test_run_renewal_loop_failure_logs_exception_class_not_message(caplog):
    """The contract-renewal background loop's top-level catch must log only
    the exception CLASS, never the raw message — an org/tenant-DB error
    string could carry PII (PII-out-of-logs invariant). DB-free: mirrors the
    equivalent loop-resilience tests in test_extraction_reaper.py /
    test_approval_escalation.py / test_audit_log_shipper.py."""
    import asyncio
    import logging
    from unittest.mock import patch

    from app.services import contract_renewal

    # Stands in for a fragment an org/tenant-DB error can carry in `str(exc)`.
    # Must never reach a log record — only the exception CLASS may.
    pii_sentinel = "SECRET_ACCOUNT_1234567890"

    async def flaky():
        raise RuntimeError(pii_sentinel)

    with (
        patch.object(contract_renewal, "notify_renewals_once", flaky),
        patch.object(contract_renewal.settings, "contract_renewal_interval_seconds", 0.01),
        caplog.at_level(logging.ERROR, logger=contract_renewal.logger.name),
    ):
        task = asyncio.create_task(contract_renewal.run_renewal_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log for the failed sweep"
    for record in errors:
        assert pii_sentinel not in record.getMessage()
    assert any("RuntimeError" in r.getMessage() for r in errors)


async def test_create_po_from_cancelled_contract_409(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        contract_id = (await _create_contract(c, vendor_id)).json()["id"]
        await c.post(f"/api/contracts/{contract_id}/cancel")
        resp = await c.post(f"/api/contracts/{contract_id}/create-po", json={})
    assert resp.status_code == 409
