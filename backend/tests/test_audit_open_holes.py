"""Behavioural coverage for the six handlers that used to write no audit row.

`tests/test_audit_append_only.py` owns the *structural* guard — it fails when a
tenant-mutating handler has no `dispatch_audit` reachable from its own source,
and its `_OPEN_AUDIT_HOLES` dict is where these six were parked while they were
still open. That guard is a source scan: it proves a call exists, not that the
row lands, not what it says, and above all not what it does NOT say.

This file is the other half. For each of the six it drives the real path and
reads the row back out of `audit_log`:

  * `workflow_definitions.create_workflow`   → `workflow.created`
  * `vendors.invite_vendor_portal_user`      → `vendor_user.invited`
  * `vendors.sync_vendors_from_erp_endpoint` → `vendor.synced_from_erp`
    (driven at `services/vendor_sync.sync_vendors_from_erp`, the chokepoint the
    endpoint is a thin ERP-adapter wrapper over — the route itself needs a
    configured ERP, and the audit contract is the service's)
  * `vendors.import_vendors_from_csv`        → `vendor.imported_csv`
  * `invoices.import_invoices_from_csv`      → `invoice.imported_csv`
  * `inspections.create_inspection`          → `quality_inspection.created`

Every case asserts the PII floor as well as the action name, because these
rows are append-only, undeletable (migration 0022's BEFORE triggers) and
shipped to a WORM store — a tax id or a supplier's temp password written here
cannot be taken back out.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.models.workflow import AuditLog


async def _audit_rows(realdb, key: str, action: str) -> list[AuditLog]:
    """Every audit row for `action` in the test tenant, oldest first."""
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.action == action)
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )


def _blob(row: AuditLog) -> str:
    """The whole row rendered as text, for "this string is nowhere in it" checks.

    Substring-searching the serialised row rather than named keys is the point:
    a future contributor adding a field to `details` cannot smuggle a secret
    past an assertion that only looked at the keys that existed today.
    """
    return json.dumps(
        {
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": str(row.entity_id),
            "details": row.details,
        }
    )


# ---------------------------------------------------------------------------
# workflow_definitions.create_workflow → workflow.created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workflow_writes_workflow_created_audit_row(realdb):
    """A workflow definition IS the approval routing rule set. Every other
    mutator in that module audited; creating one did not, so the trail could
    show a definition being edited and deleted but never coming into being."""
    info = realdb.info("a")
    async with realdb.client(key="a") as client:
        resp = await client.post(
            "/api/workflows",
            json={
                "name": "Two-step approval",
                "description": "Audit coverage",
                "steps": [
                    {"number": 1, "type": "extraction", "name": "Extract", "config": {}},
                    {"number": 2, "type": "approval", "name": "Approve", "config": {}},
                ],
            },
        )
    assert resp.status_code == 201, resp.text
    defn_id = resp.json()["id"]

    rows = await _audit_rows(realdb, "a", "workflow.created")
    assert len(rows) == 1, "exactly one row per created definition"
    row = rows[0]
    assert row.entity_type == "workflow_definition"
    assert str(row.entity_id) == defn_id
    assert row.organization_id == info.org_id
    assert row.actor_id == info.users["admin"]
    assert row.details["name"] == "Two-step approval"
    assert row.details["step_count"] == 2


@pytest.mark.asyncio
async def test_create_workflow_audit_row_commits_with_the_definition(realdb):
    """The row must be durable, not merely added to a session that the response
    path happened to commit. Read it back from a fresh connection."""
    async with realdb.client(key="a") as client:
        resp = await client.post(
            "/api/workflows",
            json={
                "name": "Durability check",
                "steps": [{"number": 1, "type": "approval", "name": "Approve", "config": {}}],
            },
        )
    assert resp.status_code == 201, resp.text
    rows = await _audit_rows(realdb, "a", "workflow.created")
    assert [r.details["name"] for r in rows] == ["Durability check"]


# ---------------------------------------------------------------------------
# inspections.create_inspection → quality_inspection.created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_inspection_writes_audit_row_without_inspector_or_quantities(realdb):
    """The 4-way-match quality gate: a `fail` here holds a payable invoice.

    Its QMS-synced sibling has always audited; the hand-entered one — the only
    inspection a human can fabricate — did not. Details mirror
    `quality_inspection.synced`: number + outcome, never the inspector's name,
    the quantities, or the free-text deviation notes.
    """
    async with realdb.client(key="a") as client:
        resp = await client.post(
            "/api/inspections",
            json={
                "inspection_number": "QI-AUDIT-1",
                "result": "fail",
                "inspector": "Dana Quality-Person",
                "accepted_quantity": "3.0000",
                "rejected_quantity": "7.0000",
                "deviation_notes": "Cracked housing on seven units",
            },
        )
    assert resp.status_code == 201, resp.text
    inspection_id = resp.json()["id"]

    rows = await _audit_rows(realdb, "a", "quality_inspection.created")
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "quality_inspection"
    assert str(row.entity_id) == inspection_id
    assert row.details == {
        "inspection_number": "QI-AUDIT-1",
        "result": "fail",
        "po_resolved": False,
        "gr_resolved": False,
    }
    blob = _blob(row)
    for leaked in ("Dana Quality-Person", "Cracked housing", "7.0000", "3.0000"):
        assert leaked not in blob, f"{leaked!r} must not reach the audit trail"


# ---------------------------------------------------------------------------
# vendors.invite_vendor_portal_user → vendor_user.invited
# ---------------------------------------------------------------------------


async def _make_vendor(realdb, key: str, name: str) -> uuid.UUID:
    from app.models.vendor import Vendor

    info = realdb.info(key)
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        vendor = Vendor(name=name, organization_id=info.org_id, status="active", source="manual")
        s.add(vendor)
        await s.commit()
        return vendor.id


@pytest.mark.asyncio
async def test_invite_portal_user_audits_without_the_email_or_temp_password(realdb, monkeypatch):
    """Minting a supplier credential is the provisioning half of the
    segregation-of-duties chain: this account can submit invoices and stage
    bank-detail changes. `vendor_user.deleted` already audited the revoke, so
    without this the trail recorded the end of a credential's life but not its
    start. The row names the actor, the vendor and the new VendorUser — never
    the login address, and never the temp password.

    The password is read out of the DELIVERED EMAIL rather than the response:
    the response no longer carries it at all (see
    `test_vendor_bank_change_provisioner_sod.py`), and the audit row must stay
    clear of the credential wherever it travelled.
    """
    vendor_id = await _make_vendor(realdb, "a", "Portal Invite Co")
    info = realdb.info("a")
    supplier_email = "ap-contact@portal-invite-co.example"

    outbox: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        outbox.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    async with realdb.client(key="a") as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": supplier_email, "full_name": "Sam Supplier"},
        )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert "temp_password" not in payload
    temp_password = next(
        line.split("Password:", 1)[1].strip()
        for line in outbox[0].body_text.splitlines()
        if line.strip().startswith("Password:")
    )
    vendor_user_id = payload["user"]["id"]

    rows = await _audit_rows(realdb, "a", "vendor_user.invited")
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "vendor"
    assert str(row.entity_id) == str(vendor_id)
    assert row.actor_id == info.users["admin"]
    assert row.details == {"vendor_user_id": vendor_user_id}

    blob = _blob(row)
    assert temp_password not in blob, "the temp password must never enter the audit trail"
    assert supplier_email not in blob
    assert "Sam Supplier" not in blob


# ---------------------------------------------------------------------------
# vendors.import_vendors_from_csv → vendor.imported_csv
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_csv_import_audits_each_created_vendor(realdb):
    """A CSV import creates PAYEE rows and bypasses every control the normal
    create path runs. One row per vendor actually created — a skipped duplicate
    creates nothing and so writes nothing."""
    from app.models.vendor import Vendor

    csv_text = (
        "name,code,email,tax_id,address\n"
        "Imported Alpha,IMP-A,alpha@example.test,12-3456789,1 Alpha Way\n"
        "Imported Beta,IMP-B,beta@example.test,98-7654321,2 Beta Road\n"
    )
    async with realdb.client(key="a") as client:
        resp = await client.post(
            "/api/vendors/import-csv",
            files={"file": ("vendors.csv", csv_text.encode(), "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 2

    rows = await _audit_rows(realdb, "a", "vendor.imported_csv")
    assert len(rows) == 2
    assert {r.details["name"] for r in rows} == {"Imported Alpha", "Imported Beta"}
    assert {r.details["code"] for r in rows} == {"IMP-A", "IMP-B"}
    assert all(r.details["source"] == "csv_import" for r in rows)
    assert all(r.entity_type == "vendor" for r in rows)

    # The audit entity_id resolves to the vendor the import actually created.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        created = {
            v.id
            for v in (await s.execute(select(Vendor).where(Vendor.code.in_(["IMP-A", "IMP-B"]))))
            .scalars()
            .all()
        }
    assert {r.entity_id for r in rows} == created

    for row in rows:
        blob = _blob(row)
        for leaked in ("12-3456789", "98-7654321", "alpha@example.test", "1 Alpha Way"):
            assert leaked not in blob, f"{leaked!r} must not reach the audit trail"


@pytest.mark.asyncio
async def test_vendor_csv_import_writes_nothing_for_a_skipped_duplicate(realdb):
    """A row that creates nothing must not manufacture audit evidence."""
    await _make_vendor(realdb, "a", "Already Here")
    async with realdb.client(key="a") as client:
        resp = await client.post(
            "/api/vendors/import-csv",
            files={"file": ("vendors.csv", b"name\nAlready Here\n", "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"imported": 0, "skipped": 1, "errors": []}
    assert await _audit_rows(realdb, "a", "vendor.imported_csv") == []


# ---------------------------------------------------------------------------
# invoices.import_invoices_from_csv → invoice.imported_csv
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_csv_import_audits_each_invoice_on_its_own_trail(realdb):
    """An import can land an invoice directly at `paid`/`done` — a state the
    workflow engine would have required an approval (and an approval signature)
    to reach. The row is keyed on the invoice's own `correlation_id`, which is
    what `GET /api/audit/invoice/{id}` joins on: keyed any other way it would
    be invisible on the very invoice it describes.
    """
    from app.models.invoice import Invoice

    csv_text = (
        "invoice_number,vendor_name,amount,status\n"
        "INV-IMP-1,Historic Supplier,1250.00,paid\n"
        "INV-IMP-2,Historic Supplier,90.00,new\n"
    )
    async with realdb.client(key="a") as client:
        resp = await client.post(
            "/api/invoices/import-csv",
            files={"file": ("invoices.csv", csv_text.encode(), "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 2

    rows = await _audit_rows(realdb, "a", "invoice.imported_csv")
    assert len(rows) == 2
    by_number = {r.details["invoice_number"]: r for r in rows}
    assert set(by_number) == {"INV-IMP-1", "INV-IMP-2"}
    assert by_number["INV-IMP-1"].details["status"] == "paid"
    assert by_number["INV-IMP-2"].details["status"] == "new"
    assert all(r.entity_type == "invoice" for r in rows)
    assert all(r.details["source"] == "csv_import" for r in rows)

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        invoices = {
            inv.invoice_number: inv
            for inv in (
                await s.execute(
                    select(Invoice).where(Invoice.invoice_number.in_(["INV-IMP-1", "INV-IMP-2"]))
                )
            )
            .scalars()
            .all()
        }
    for number, row in by_number.items():
        assert row.entity_id == invoices[number].id
        # The join key the per-invoice audit view uses.
        assert row.correlation_id == invoices[number].correlation_id


@pytest.mark.asyncio
async def test_invoice_csv_import_audits_the_vendor_stub_it_mints(realdb):
    """An import naming an unknown supplier MINTS a payee row. Without its own
    row the only trace of a new payee entering the tenant is the invoice that
    referenced it."""
    from app.models.vendor import Vendor

    async with realdb.client(key="a") as client:
        resp = await client.post(
            "/api/invoices/import-csv",
            files={
                "file": (
                    "invoices.csv",
                    b"invoice_number,vendor_name,amount\nINV-STUB-1,Brand New Payee,10.00\n",
                    "text/csv",
                )
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1

    rows = await _audit_rows(realdb, "a", "vendor.imported_csv")
    assert len(rows) == 1
    assert rows[0].details["name"] == "Brand New Payee"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        vendor = (
            await s.execute(select(Vendor).where(Vendor.name == "Brand New Payee"))
        ).scalar_one()
    assert rows[0].entity_id == vendor.id


# ---------------------------------------------------------------------------
# vendor_sync.sync_vendors_from_erp → vendor.synced_from_erp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erp_vendor_sync_audits_create_update_and_link(realdb):
    """Three distinct things happen in one pull, and the trail has to tell them
    apart: a brand-new payee, a refresh of one already linked to the ERP, and
    the adoption of a manually-created vendor by an ERP id (which can also
    promote it out of `unverified`). A record the pull leaves byte-identical
    writes nothing."""
    from app.models.vendor import Vendor
    from app.services.vendor_sync import sync_vendors_from_erp

    info = realdb.info("a")
    actor = info.users["admin"]
    mk = realdb.sessionmaker("a")

    async with mk() as s:
        s.add(
            Vendor(
                name="Manual Co",
                organization_id=info.org_id,
                status="unverified",
                source="manual",
            )
        )
        await s.commit()

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            info.org_id,
            [
                {"erp_vendor_id": "ERP-NEW", "name": "Fresh From ERP", "code": "EN-1"},
                {"erp_vendor_id": "ERP-LINK", "name": "Manual Co", "code": "MC-1"},
            ],
            actor_id=actor,
        )
        await s.commit()
    assert {k: result[k] for k in ("created", "updated", "unchanged", "conflicts")} == {
        "created": 1,
        "updated": 1,
        "unchanged": 0,
        "conflicts": [],
    }

    rows = await _audit_rows(realdb, "a", "vendor.synced_from_erp")
    assert {r.details["change"] for r in rows} == {"created", "linked"}
    assert {r.details["erp_vendor_id"] for r in rows} == {"ERP-NEW", "ERP-LINK"}
    assert all(r.actor_id == actor for r in rows)
    assert all(r.entity_type == "vendor" for r in rows)

    # `entity_id` must resolve to the vendor row, not be a NULL placeholder —
    # a created vendor's id only exists after the flush.
    async with mk() as s:
        synced = {
            v.id
            for v in (
                await s.execute(
                    select(Vendor).where(Vendor.erp_vendor_id.in_(["ERP-NEW", "ERP-LINK"]))
                )
            )
            .scalars()
            .all()
        }
    assert {r.entity_id for r in rows} == synced
    assert len(synced) == 2

    # A second identical pull changes nothing, so it must add no evidence.
    async with mk() as s:
        again = await sync_vendors_from_erp(
            s,
            info.org_id,
            [
                {"erp_vendor_id": "ERP-NEW", "name": "Fresh From ERP", "code": "EN-1"},
                {"erp_vendor_id": "ERP-LINK", "name": "Manual Co", "code": "MC-1"},
            ],
            actor_id=actor,
        )
        await s.commit()
    assert again["unchanged"] == 2
    assert len(await _audit_rows(realdb, "a", "vendor.synced_from_erp")) == 2


@pytest.mark.asyncio
async def test_erp_vendor_sync_audit_keeps_tax_id_and_contact_details_out(realdb):
    """The ERP payload carries `tax_id`, `email`, `phone` and `address`, and the
    sync writes all four onto the vendor row. None may reach the trail."""
    from app.services.vendor_sync import sync_vendors_from_erp

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        await sync_vendors_from_erp(
            s,
            info.org_id,
            [
                {
                    "erp_vendor_id": "ERP-PII",
                    "name": "Sensitive Supplier",
                    "code": "SS-1",
                    "email": "billing@sensitive.example",
                    "phone": "+1-555-0100",
                    "address": "9 Confidential Row",
                    "tax_id": "55-5555555",
                }
            ],
            actor_id=info.users["admin"],
        )
        await s.commit()

    rows = await _audit_rows(realdb, "a", "vendor.synced_from_erp")
    assert len(rows) == 1
    blob = _blob(rows[0])
    for leaked in (
        "billing@sensitive.example",
        "+1-555-0100",
        "9 Confidential Row",
        "55-5555555",
    ):
        assert leaked not in blob, f"{leaked!r} must not reach the audit trail"
