"""The dashboard's first-run fields: org-wide emptiness and the setup checklist.

Two defects from #635, one decision (`docs/decisions.md` §309):

* **Emptiness is a property of the workspace, not of the selected entity.**
  The dashboard decided "brand-new workspace" from `total_invoices`, which is
  entity-scoped — so an established tenant that added a subsidiary was greeted
  with "upload your first invoice" on switching to it.
  `organization_has_invoices` answers the workspace question unscoped.
* **An admin on an empty workspace gets a setup checklist**, each step ticked
  only from a fact another feature already owns (`services/onboarding.py`).
  Nothing here is stored; each test flips the OWNING data and watches the flag
  follow it, because a flag that could be ticked any other way would be a tick
  that lies.

Runs against the `realdb` harness (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from app.config import settings as app_settings
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.vendor import Vendor
from app.models.workflow import WorkflowDefinition
from app.services.onboarding import compute_onboarding

pytestmark = pytest.mark.asyncio

TENANT = "a"

ALL_FALSE_BUT_USERS = {
    "company": False,
    # The harness seeds one user per system role, so this tenant always has
    # more than one; the single-user case is pinned directly below.
    "users": True,
    "security": False,
    "erp": False,
    "vendors": False,
    "workflow": False,
    "email_intake": False,
}


async def _set_settings(realdb, value: dict) -> None:
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == realdb.info(TENANT).org_id)
            .values(settings=value)
        )
        await s.commit()


async def _add_invoice(realdb, *, entity_id: uuid.UUID | None = None) -> None:
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Invoice(
                organization_id=realdb.info(TENANT).org_id,
                entity_id=entity_id,
                invoice_number=f"ONB-{uuid.uuid4().hex[:8]}",
                vendor_name="First Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.new,
            )
        )
        await s.commit()


async def test_admin_on_an_empty_workspace_gets_the_checklist(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()

    assert body["total_invoices"] == 0
    assert body["organization_has_invoices"] is False
    assert body["onboarding"] == ALL_FALSE_BUT_USERS


@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
async def test_a_non_admin_gets_no_checklist(realdb, role):
    """Setup is the admin's job, and the checklist describes the tenant's
    configuration — another role reads the empty state, not the steps."""
    async with realdb.client(key=TENANT, role=role) as c:
        body = (await c.get("/api/dashboard")).json()

    assert body["organization_has_invoices"] is False
    assert body["onboarding"] is None


async def test_each_flag_follows_the_data_that_owns_it(realdb, monkeypatch):
    org_id = realdb.info(TENANT).org_id
    monkeypatch.setattr(app_settings, "email_intake_domain", "intake.example.test")

    await _set_settings(
        realdb,
        {
            # The organization NAME is set at signup, so a profile counts as
            # filled only when something beyond it is.
            "company": {"name": "Acme", "address": "1 Main St"},
            "mfa": {"required": True},
            "erp": {"type": "netsuite", "integration_method": "direct"},
            "email_intake": {"token": "abc123", "enabled": True},
        },
    )
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        s.add(Vendor(organization_id=org_id, name="Paper Co"))
        defn = WorkflowDefinition(
            organization_id=org_id, name="Default Workflow", steps_config={}, is_default=True
        )
        s.add(defn)
        await s.commit()
        defn_id = defn.id

    async with realdb.client(key=TENANT, role="admin") as c:
        progress = (await c.get("/api/dashboard")).json()["onboarding"]
    # One definition, never saved since it was created: that is the default
    # every tenant is provisioned with, and not evidence anyone reviewed it.
    assert progress == {flag: flag != "workflow" for flag in ALL_FALSE_BUT_USERS}

    # Saving it — in a later transaction, as the builder does — is the signal.
    # `updated_at` is `onupdate=now()`, and Postgres `now()` is per transaction.
    async with mk() as s:
        saved = await s.get(WorkflowDefinition, defn_id)
        saved.name = "Two approvers over 5k"
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        dash = (await c.get("/api/dashboard")).json()["onboarding"]
        alone = (await c.get("/api/dashboard/onboarding")).json()

    assert all(dash.values()), dash
    # The settings page's endpoint is the same derivation, not a second one.
    assert alone == {**dash, "organization_has_invoices": False}


async def test_blank_profile_fields_and_mock_erp_do_not_count(realdb):
    await _set_settings(
        realdb,
        {
            "company": {"name": "Acme", "address": "  ", "tax_id": "", "logo_url": "x.png"},
            "mfa": {"required": False},
            "sso": {"enabled": False},
            "erp": {"type": "mock", "integration_method": "direct"},
        },
    )
    async with realdb.client(key=TENANT, role="admin") as c:
        progress = (await c.get("/api/dashboard")).json()["onboarding"]

    assert progress == ALL_FALSE_BUT_USERS


async def test_sso_counts_as_securing_sign_in(realdb):
    await _set_settings(realdb, {"sso": {"enabled": True}})
    async with realdb.client(key=TENANT, role="admin") as c:
        progress = (await c.get("/api/dashboard")).json()["onboarding"]
    assert progress["security"] is True


async def test_a_lone_admin_has_not_invited_anyone(realdb):
    """Signup creates exactly one user. An org whose only member is its
    admin has invited nobody — computed against an org id with no other
    member, since the harness tenants carry one user per role."""
    async with realdb.sessionmaker(TENANT)() as db, realdb.control_sessionmaker()() as ctrl:
        progress = await compute_onboarding(
            db=db, control_db=ctrl, org=SimpleNamespace(id=uuid.uuid4(), settings={})
        )
    assert progress.users is False


async def test_the_checklist_retires_with_the_first_invoice(realdb):
    await _add_invoice(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()

    assert body["organization_has_invoices"] is True
    assert body["onboarding"] is None


@pytest.mark.plan("scale")
async def test_a_new_entity_on_an_established_workspace_is_not_a_new_workspace(realdb):
    """The #635 case: switching to a subsidiary with no invoices yet must not
    read as a brand-new workspace — the scoped count is zero, the org's is not."""
    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.post("/api/entities", json={"name": "New Sub", "slug": "new-sub"})
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    await _add_invoice(realdb, entity_id=uuid.UUID(default_id))

    async with realdb.client(key=TENANT, role="admin") as c:
        scoped = (await c.get("/api/dashboard", headers={"X-Entity-ID": sub_id})).json()

    assert scoped["total_invoices"] == 0
    assert scoped["organization_has_invoices"] is True
    assert scoped["onboarding"] is None


@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
async def test_the_onboarding_endpoint_is_admin_only(realdb, role):
    async with realdb.client(key=TENANT, role=role) as c:
        r = await c.get("/api/dashboard/onboarding")
    assert r.status_code == 403
