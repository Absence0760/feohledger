"""By-id routes on `EntityMixin` models honour `X-Entity-ID` — the sweep.

Multi-entity Phase 2 scoped every LIST to the selected subsidiary, but most
routers resolved their by-id routes on the primary key alone, so the selector
was advisory exactly where it mattered: a caller scoped to subsidiary B could
read, edit, approve, delete or pay-adjacent-mutate A's invoice, vendor,
contract, expense, workflow definition, experiment, card, inspection,
suggestion or exception by holding its id. `GET /api/purchase-orders/{id}`
(`_get_scoped_po`) and the payments by-id routes had already been closed; this
file pins the rest, via `app.tenant.ensure_in_entity_scope`.

For every route the contract is the same (`tests/entity_scope_probe.py`): an
out-of-scope id is a 404 byte-identical to the one an unknown id gets (no
enumeration oracle), nothing is written, and the owning entity plus the
consolidated view still reach the row.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.adaptive_suggestion import WorkflowSuggestion
from app.models.contract import Contract, ContractStatus
from app.models.exception import Exception as APException
from app.models.expense import Expense, ExpensePolicy, ExpensePreapproval, ExpenseReport
from app.models.invoice import Invoice, InvoiceStatus
from app.models.quality_inspection import QualityInspection
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.virtual_card import VirtualCard
from app.models.workflow import WorkflowDefinition
from app.models.workflow_experiment import WorkflowExperiment
from tests.entity_scope_probe import assert_out_of_scope_404, two_entities

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = pytest.mark.plan("scale")

TENANT = "a"
PDF = {"file": ("doc.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")}


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _add(mk, row):
    async with mk() as s:
        s.add(row)
        await s.commit()
        return row.id


async def _reload(mk, model, row_id):
    async with mk() as s:
        return (await s.execute(select(model).where(model.id == row_id))).scalar_one_or_none()


async def _setup(realdb, c, slug: str):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    default_id, other_id = await two_entities(c, slug=f"{slug}-{_tag()}")
    return org_id, mk, default_id, other_id, uuid.UUID(default_id)


async def _probe_all(c, other_id, routes):
    for route in routes:
        method, path, row_id, *rest = route
        kw = rest[0] if rest else {}
        await assert_out_of_scope_404(c, method, path, row_id, entity_id=other_id, **kw)


async def test_invoice_by_id_routes_are_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id, dflt = await _setup(realdb, c, "inv")
        inv_id = await _add(
            mk,
            Invoice(
                invoice_number=f"INV-{_tag()}",
                vendor_name="Acme Supplies",
                amount=Decimal("250.00"),
                status=InvoiceStatus.ready_for_review,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        iid = str(inv_id)
        await _probe_all(
            c,
            other_id,
            [
                # api/invoices.py
                ("GET", "/api/invoices/{id}", iid),
                ("GET", "/api/invoices/{id}/priors", iid),
                ("GET", "/api/invoices/{id}/summary", iid),
                ("POST", "/api/invoices/{id}/summary/regenerate", iid),
                ("GET", "/api/invoices/{id}/einvoice", iid),
                (
                    "POST",
                    "/api/invoices/{id}/peppol-send",
                    iid,
                    {"json": {"receiver_scheme": "0088", "receiver_value": "5790000435975"}},
                ),
                ("GET", "/api/invoices/{id}/line-items", iid),
                ("PUT", "/api/invoices/{id}/line-items", iid, {"json": []}),
                ("POST", "/api/invoices/{id}/file", iid, {"files": PDF}),
                ("PUT", "/api/invoices/{id}/file", iid, {"files": PDF}),
                ("DELETE", "/api/invoices/{id}/file", iid),
                ("PATCH", "/api/invoices/{id}", iid, {"json": {"vendor_name": "hijacked"}}),
                (
                    "POST",
                    "/api/invoices/{id}/link-contract",
                    iid,
                    {"json": {"contract_id": str(uuid.uuid4())}},
                ),
                ("POST", "/api/invoices/{id}/unlink-contract", iid),
                (
                    "POST",
                    "/api/invoices/{id}/route-intercompany",
                    iid,
                    {"json": {"counterparty_entity_id": other_id}},
                ),
                ("GET", "/api/invoices/{id}/chat", iid),
                ("POST", "/api/invoices/{id}/chat", iid, {"json": {"body": "hello"}}),
                (
                    "POST",
                    "/api/invoices/{id}/chat/attachments",
                    iid,
                    {"files": PDF, "data": {"body": "x"}},
                ),
                ("POST", "/api/invoices/{id}/chat/resolve", iid),
                ("POST", "/api/invoices/{id}/chat/reopen", iid),
                # api/workflow.py
                ("POST", "/api/invoices/{id}/extract", iid),
                ("POST", "/api/invoices/{id}/reset-extraction", iid),
                (
                    "POST",
                    "/api/invoices/{id}/assign",
                    iid,
                    {"json": {"user_id": str(uuid.uuid4())}},
                ),
                ("POST", "/api/invoices/{id}/approve", iid),
                ("POST", "/api/invoices/{id}/reject", iid, {"json": {"reason": "no"}}),
                ("POST", "/api/invoices/{id}/resubmit", iid),
                ("POST", "/api/invoices/{id}/send-to-erp", iid),
                ("POST", "/api/invoices/{id}/retry-erp", iid),
                ("POST", "/api/invoices/{id}/complete", iid),
                ("GET", "/api/invoices/{id}/export", iid),
                ("GET", "/api/invoices/{id}/workflow", iid),
                ("GET", "/api/invoices/{id}/audit-log", iid),
                ("GET", "/api/invoices/{id}/extraction", iid),
                # api/audit.py
                ("GET", "/api/audit/invoice/{id}", iid),
                ("GET", "/api/audit/invoice/{id}/verify-signatures", iid),
                # last: the destructive one
                ("DELETE", "/api/invoices/{id}", iid),
            ],
        )

        row = await _reload(mk, Invoice, inv_id)
        assert row is not None, "an out-of-scope DELETE removed the invoice"
        assert row.status == InvoiceStatus.ready_for_review
        assert row.vendor_name == "Acme Supplies"
        assert row.counterparty_entity_id is None

        own = {"X-Entity-ID": default_id}
        assert (await c.get(f"/api/invoices/{iid}", headers=own)).status_code == 200
        assert (await c.get(f"/api/invoices/{iid}")).status_code == 200
        assert (await c.get(f"/api/audit/invoice/{iid}", headers=own)).status_code == 200


async def test_invoice_bulk_delete_is_entity_scoped(realdb):
    """`POST /bulk/delete` takes ids too, and resolved them on the primary key
    alone — so a caller scoped to one subsidiary could delete another's invoice
    by id. It now scopes and row-locks like `bulk/status`; an out-of-scope id
    is reported as skipped exactly like an unknown one, and survives."""
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id, dflt = await _setup(realdb, c, "bulkdel")
        theirs = await _add(
            mk,
            Invoice(
                invoice_number=f"INV-{_tag()}",
                vendor_name="Acme Supplies",
                amount=Decimal("250.00"),
                status=InvoiceStatus.new,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        mine = await _add(
            mk,
            Invoice(
                invoice_number=f"INV-{_tag()}",
                vendor_name="Acme Supplies",
                amount=Decimal("250.00"),
                status=InvoiceStatus.new,
                organization_id=org_id,
                entity_id=uuid.UUID(other_id),
            ),
        )
        unknown = uuid.uuid4()

        resp = await c.post(
            "/api/invoices/bulk/delete",
            json={"ids": [str(theirs), str(mine), str(unknown)]},
            headers={"X-Entity-ID": other_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted"] == 1
        assert sorted(body["skipped"]) == sorted([str(theirs), str(unknown)])

    assert await _reload(mk, Invoice, theirs) is not None, "another entity's invoice survives"
    assert await _reload(mk, Invoice, mine) is None


async def test_vendor_by_id_routes_are_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id, dflt = await _setup(realdb, c, "ven")
        vendor_id = await _add(
            mk, Vendor(name=f"Scoped Vendor {_tag()}", organization_id=org_id, entity_id=dflt)
        )
        vid = str(vendor_id)
        change_id = await _add(
            mk,
            VendorChangeRequest(
                vendor_id=vendor_id,
                organization_id=org_id,
                change_type="bank_details",
                proposed_value={"bank_name": "Elsewhere"},
            ),
        )
        await _probe_all(
            c,
            other_id,
            [
                ("GET", "/api/vendors/{id}", vid),
                ("PATCH", "/api/vendors/{id}", vid, {"json": {"name": "hijacked"}}),
                (
                    "POST",
                    "/api/vendors/{id}/bank-change",
                    vid,
                    {"json": {"bank_details": {"bank_name": "Evil"}}},
                ),
                ("POST", "/api/vendors/{id}/screen", vid),
                ("GET", "/api/vendors/{id}/screening-history", vid),
                ("POST", "/api/vendors/{id}/block", vid),
                ("POST", "/api/vendors/{id}/unblock", vid),
                ("POST", "/api/vendors/{id}/verify", vid),
                ("POST", "/api/vendors/{id}/reject", vid),
                ("GET", "/api/vendors/{id}/portal-users", vid),
                (
                    "POST",
                    "/api/vendors/{id}/portal-users",
                    vid,
                    {"json": {"email": f"p-{_tag()}@example.com", "full_name": "P"}},
                ),
                ("DELETE", f"/api/vendors/{{id}}/portal-users/{uuid.uuid4()}", vid),
                (
                    "POST",
                    f"/api/vendors/{{id}}/portal-users/{uuid.uuid4()}/reset-password",
                    vid,
                ),
                ("GET", "/api/vendors/{id}/change-requests", vid),
                ("POST", "/api/vendors/change-requests/{id}/approve", str(change_id)),
                ("POST", "/api/vendors/change-requests/{id}/reject", str(change_id)),
                # api/tax.py + api/vendor_risk.py
                ("PATCH", "/api/tax/vendors/{id}/w9", vid, {"json": {"is_1099_eligible": True}}),
                ("POST", "/api/tax/vendors/{id}/w9", vid, {"files": PDF}),
                ("POST", "/api/tax/vendors/{id}/tin-verify", vid, {"json": {}}),
                ("GET", "/api/tax/vendors/{id}/1099", vid, {"params": {"year": 2026}}),
                ("GET", "/api/vendors/{id}/risk", vid),
                ("POST", "/api/vendors/{id}/risk/recompute", vid),
                ("DELETE", "/api/vendors/{id}", vid),
            ],
        )

        row = await _reload(mk, Vendor, vendor_id)
        assert row is not None and not row.name.startswith("hijacked")
        change = await _reload(mk, VendorChangeRequest, change_id)
        assert change.status == "pending"

        # Unstamped vendors stay reachable from every entity (vendor_matching's rule).
        unstamped = await _add(
            mk, Vendor(name=f"Unstamped {_tag()}", organization_id=org_id, entity_id=None)
        )
        own = {"X-Entity-ID": default_id}
        assert (await c.get(f"/api/vendors/{vid}", headers=own)).status_code == 200
        assert (await c.get(f"/api/vendors/{vid}")).status_code == 200
        assert (
            await c.get(f"/api/vendors/{unstamped}", headers={"X-Entity-ID": other_id})
        ).status_code == 200


async def test_contract_by_id_routes_are_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id, dflt = await _setup(realdb, c, "con")
        vendor_id = await _add(
            mk, Vendor(name=f"Contract Vendor {_tag()}", organization_id=org_id, entity_id=dflt)
        )
        contract_id = await _add(
            mk,
            Contract(
                contract_number=f"CT-{_tag()}",
                vendor_id=vendor_id,
                status=ContractStatus.draft,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        cid = str(contract_id)
        await _probe_all(
            c,
            other_id,
            [
                ("GET", "/api/contracts/{id}", cid),
                ("PATCH", "/api/contracts/{id}", cid, {"json": {"title": "hijacked"}}),
                ("POST", "/api/contracts/{id}/upload", cid, {"files": PDF}),
                ("POST", "/api/contracts/{id}/activate", cid),
                ("POST", "/api/contracts/{id}/terminate", cid),
                ("POST", "/api/contracts/{id}/cancel", cid),
                ("POST", "/api/contracts/{id}/renew", cid, {"json": {"end_date": "2030-01-01"}}),
                ("POST", "/api/contracts/{id}/create-po", cid, {"json": {}}),
                ("DELETE", "/api/contracts/{id}", cid),
            ],
        )
        row = await _reload(mk, Contract, contract_id)
        assert row is not None and row.status == ContractStatus.draft

        own = {"X-Entity-ID": default_id}
        assert (await c.get(f"/api/contracts/{cid}", headers=own)).status_code == 200
        assert (await c.get(f"/api/contracts/{cid}")).status_code == 200


async def test_expense_by_id_routes_are_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id, dflt = await _setup(realdb, c, "exp")
        employee = uuid.uuid4()
        policy_id = await _add(
            mk, ExpensePolicy(name="Travel", organization_id=org_id, entity_id=dflt)
        )
        pre_id = await _add(
            mk,
            ExpensePreapproval(
                requester_user_id=employee,
                title="Conference",
                estimated_amount=Decimal("900.00"),
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        expense_id = await _add(
            mk,
            Expense(
                expense_date=date(2026, 9, 1),
                amount=Decimal("42.00"),
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        report_id = await _add(
            mk,
            ExpenseReport(
                report_number=f"ER-{_tag()}",
                employee_user_id=employee,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        pid, prid, eid, rid = str(policy_id), str(pre_id), str(expense_id), str(report_id)
        await _probe_all(
            c,
            other_id,
            [
                ("GET", "/api/expense-policies/{id}", pid),
                ("PATCH", "/api/expense-policies/{id}", pid, {"json": {"name": "hijacked"}}),
                ("DELETE", "/api/expense-policies/{id}", pid),
                ("GET", "/api/expense-preapprovals/{id}", prid),
                ("POST", "/api/expense-preapprovals/{id}/approve", prid),
                ("POST", "/api/expense-preapprovals/{id}/reject", prid),
                ("POST", "/api/expenses/{id}/receipt", eid, {"files": PDF}),
                ("GET", "/api/expenses/{id}", eid),
                ("PATCH", "/api/expenses/{id}", eid, {"json": {"description": "hijacked"}}),
                ("DELETE", "/api/expenses/{id}", eid),
                ("GET", "/api/expense-reports/{id}", rid),
                ("GET", "/api/expense-reports/{id}/summary", rid),
                ("PATCH", "/api/expense-reports/{id}", rid, {"json": {"title": "hijacked"}}),
                ("POST", "/api/expense-reports/{id}/expenses", rid, {"json": {"expense_ids": []}}),
                ("POST", "/api/expense-reports/{id}/submit", rid),
                ("POST", "/api/expense-reports/{id}/approve", rid),
                ("POST", "/api/expense-reports/{id}/reject", rid),
            ],
        )
        assert (await _reload(mk, ExpensePolicy, policy_id)).name == "Travel"
        assert (await _reload(mk, ExpensePreapproval, pre_id)).decided_by is None
        assert (await _reload(mk, Expense, expense_id)) is not None
        assert (await _reload(mk, ExpenseReport, report_id)).submitted_at is None

        own = {"X-Entity-ID": default_id}
        for path in (
            f"/api/expense-policies/{pid}",
            f"/api/expense-preapprovals/{prid}",
            f"/api/expenses/{eid}",
            f"/api/expense-reports/{rid}",
        ):
            assert (await c.get(path, headers=own)).status_code == 200, path
            assert (await c.get(path)).status_code == 200, path


async def test_workflow_definition_and_experiment_by_id_routes_are_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id, dflt = await _setup(realdb, c, "wf")
        steps = {"steps": [{"type": "approval"}]}
        wf_id = await _add(
            mk,
            WorkflowDefinition(
                name=f"Scoped WF {_tag()}",
                steps_config=steps,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        exp_id = await _add(
            mk,
            WorkflowExperiment(
                name=f"Scoped exp {_tag()}",
                workflow_definition_id=wf_id,
                config_a=steps,
                config_b=steps,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        wid, xid = str(wf_id), str(exp_id)
        await _probe_all(
            c,
            other_id,
            [
                ("GET", "/api/workflows/{id}", wid),
                ("PATCH", "/api/workflows/{id}", wid, {"json": {"name": "hijacked"}}),
                ("GET", "/api/workflows/{id}/versions", wid),
                ("POST", "/api/workflows/{id}/versions", wid, {"json": {}}),
                ("POST", f"/api/workflows/{{id}}/restore/{uuid.uuid4()}", wid),
                ("GET", "/api/workflows/{id}/versions/diff", wid, {"params": {"from": "1"}}),
                ("POST", "/api/workflows/{id}/simulate", wid, {"json": {}}),
                ("GET", "/api/workflows/{id}/export", wid),
                ("PATCH", "/api/experiments/{id}", xid, {"json": {"name": "hijacked"}}),
                ("POST", "/api/experiments/{id}/start", xid),
                ("POST", "/api/experiments/{id}/stop", xid),
                ("POST", "/api/experiments/{id}/conclude", xid),
                ("GET", "/api/experiments/{id}/results", xid),
                ("DELETE", "/api/experiments/{id}", xid),
                ("DELETE", "/api/workflows/{id}", wid),
            ],
        )
        assert (await _reload(mk, WorkflowDefinition, wf_id)).name.startswith("Scoped WF")
        exp = await _reload(mk, WorkflowExperiment, exp_id)
        assert exp is not None and exp.status == "draft"

        own = {"X-Entity-ID": default_id}
        assert (await c.get(f"/api/workflows/{wid}", headers=own)).status_code == 200
        assert (await c.get(f"/api/workflows/{wid}")).status_code == 200
        assert (await c.get(f"/api/experiments/{xid}/results", headers=own)).status_code == 200


async def test_card_inspection_suggestion_and_exception_routes_are_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id, dflt = await _setup(realdb, c, "misc")
        inv_id = await _add(
            mk,
            Invoice(
                invoice_number=f"INV-{_tag()}",
                vendor_name="Card Vendor",
                amount=Decimal("80.00"),
                status=InvoiceStatus.approved,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        card_id = await _add(
            mk,
            VirtualCard(
                invoice_id=inv_id,
                card_provider="mock",
                provider_card_id=f"mock-{_tag()}",
                amount_limit=Decimal("80.00"),
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        inspection_id = await _add(
            mk,
            QualityInspection(
                inspection_number=f"QI-{_tag()}", organization_id=org_id, entity_id=dflt
            ),
        )
        suggestion_id = await _add(
            mk,
            WorkflowSuggestion(
                kind="approval_threshold",
                dedupe_key=f"k-{_tag()}",
                title="Raise the threshold",
                payload={},
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        exc_id = await _add(
            mk,
            APException(
                exception_type="missing_data",
                invoice_id=inv_id,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        await _probe_all(
            c,
            other_id,
            [
                ("GET", "/api/cards/{id}/details", str(card_id)),
                ("POST", "/api/cards/{id}/cancel", str(card_id)),
                ("GET", "/api/inspections/{id}", str(inspection_id)),
                ("POST", "/api/adaptive/suggestions/{id}/dismiss", str(suggestion_id)),
                ("POST", "/api/exceptions/{id}/agent-resolve", str(exc_id)),
            ],
        )
        card = await _reload(mk, VirtualCard, card_id)
        assert str(card.status) != "cancelled"
        assert (await _reload(mk, WorkflowSuggestion, suggestion_id)).status == "open"

        own = {"X-Entity-ID": default_id}
        assert (await c.get(f"/api/inspections/{inspection_id}", headers=own)).status_code == 200
        assert (await c.get(f"/api/inspections/{inspection_id}")).status_code == 200
