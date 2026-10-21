"""An administrator can close an unused workspace, which releases its address.

`users.email` is globally unique (decisions §291). Before this, someone who
signed up, abandoned the trial, and was later added to another workspace with
the same address met `email_in_other_workspace` with no way out but support,
because deleting a workspace was operator-only. `POST /api/organization/close`
(decisions §294) lets the abandoned workspace's administrator close it; it is
refused, fail-closed, for any workspace that holds something that must be kept.

Layers:

* static — every guard names a real tenant table, the codes are unique, and
  the SPA localizes every one of them;
* the guard matrix through the read-only preflight on the harness tenant: one
  real row per tenant check (built generically from the model, so a new check
  is covered the day it lands), the rows that must NOT block, and each
  control-plane blocker;
* the closure itself on a throwaway tenant provisioned for the test: the dead
  end reproduced and then released, the confirmation, the step-up, the refusal
  under lock (with its audit row), the concurrency lock, the audit-archive
  failure that must delete nothing, the retry of a half-finished closure, and
  the record that outlives the tenant.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pyotp
import pytest
import pytest_asyncio
from sqlalchemy import Enum as SAEnum
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.types import JSON, Boolean, Date, DateTime, Integer, Numeric, String, Text

from app.database import _make_tenant_url
from app.models import Base
from app.models.organization import Organization
from app.models.user import User
from app.models.workflow import AuditLog
from app.models.workspace_closure import CLOSURE_COMPLETED, CLOSURE_STARTED, WorkspaceClosure
from app.services import tenant_deletion, workspace_closure
from app.services.tenant_provisioning import CONTROL_TABLES, provision_tenant
from tests.conftest import TenantInfo

FRONTEND_BLOCKER_MESSAGES = (
    Path(__file__).resolve().parents[2] / "frontend/src/lib/types/workspaceClosure.ts"
)


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


def test_every_tenant_check_names_a_tenant_table():
    for check in workspace_closure.TENANT_CHECKS:
        assert check.table in Base.metadata.tables, check.table
        assert check.table not in CONTROL_TABLES, check.table
        # The SQL reads the table it says it does — the lock list is derived
        # from `table`, so the two must never disagree.
        assert f"FROM {check.table}" in check.sql


def test_blocker_codes_are_unique():
    assert len(workspace_closure.BLOCKERS) == len(set(workspace_closure.BLOCKERS))


def test_every_tenant_table_is_guarded_or_allowed_exactly_once():
    """A new tenant table cannot reach a closure without someone deciding
    whether it is a record that must be kept (the shape of
    `tenant_deletion.CONTROL_TABLES_EXEMPT`)."""
    tenant_tables = {t for t in Base.metadata.tables if t not in CONTROL_TABLES}
    guarded = {c.table for c in workspace_closure.TENANT_CHECKS}
    allowed = set(workspace_closure.CLOSURE_ALLOWED)
    assert not guarded & allowed, "a table is both guarded and allowed"
    assert guarded | allowed == tenant_tables, "unclassified: " + ", ".join(
        sorted(tenant_tables - guarded - allowed)
    ) or "stale: " + ", ".join(sorted((guarded | allowed) - tenant_tables))
    for table, reason in workspace_closure.CLOSURE_ALLOWED.items():
        assert reason.strip(), table


def test_a_child_table_names_a_guarded_parent():
    guarded = {c.table for c in workspace_closure.TENANT_CHECKS}
    for table, reason in workspace_closure.CLOSURE_ALLOWED.items():
        if reason.startswith("Child rows of `"):
            parent = reason.split("`")[1]
            assert parent in guarded, f"{table} points at {parent}, which has no guard"


def test_the_page_labels_every_inventory_class():
    source = FRONTEND_BLOCKER_MESSAGES.read_text()
    listed = set(re.findall(r"^\s+([a-z_]+): '", source, re.MULTILINE))
    codes = {code for code, _ in workspace_closure.INVENTORY_COUNTS}
    assert codes | {workspace_closure.INVENTORY_INACTIVE_USERS} <= listed


def test_the_page_localizes_every_blocker():
    """A code the SPA has no sentence for would render as a bare code."""
    source = FRONTEND_BLOCKER_MESSAGES.read_text()
    listed = set(re.findall(r"^\s+([a-z_]+): '", source, re.MULTILINE))
    assert set(workspace_closure.BLOCKERS) <= listed


# ---------------------------------------------------------------------------
# A real row for any tenant table, built from the model
# ---------------------------------------------------------------------------


def _filler(column):
    t = column.type
    if isinstance(t, SAEnum):
        return t.enums[0]
    if isinstance(t, UUID):
        return uuid.uuid4()
    if isinstance(t, Boolean):
        return False
    if isinstance(t, Numeric):
        return Decimal("1.00")
    if isinstance(t, Integer):
        return 2026
    if isinstance(t, DateTime):
        return datetime.now(UTC)
    if isinstance(t, Date):
        return date.today()
    if isinstance(t, JSONB | JSON):
        return {}
    if isinstance(t, String | Text):
        length = getattr(t, "length", None) or 32
        return uuid.uuid4().hex[: min(length, 32)]
    raise AssertionError(f"no filler for {column.table.name}.{column.name} ({t!r})")


async def _insert_row(session, table_name: str, org_id: uuid.UUID, **overrides) -> uuid.UUID:
    """Insert one valid row: every required column filled, parents created."""
    table = Base.metadata.tables[table_name]
    values: dict = {}
    for column in table.columns:
        if column.name in overrides:
            continue
        if column.name == "organization_id":
            values[column.name] = org_id
            continue
        required = (
            not column.nullable
            and column.default is None
            and column.server_default is None
            and not column.primary_key
        )
        if not required:
            continue
        fks = list(column.foreign_keys)
        if fks:
            values[column.name] = await _insert_row(session, fks[0].column.table.name, org_id)
        else:
            values[column.name] = _filler(column)
    values.update(overrides)
    row_id = values.setdefault("id", uuid.uuid4())
    await session.execute(insert(table).values(**values))
    return row_id


# ---------------------------------------------------------------------------
# The guard matrix, through the read-only preflight on harness tenant A
# ---------------------------------------------------------------------------

#: Overrides that make a conditional check's row a blocking one.
BLOCKING_OVERRIDES = {
    workspace_closure.BLOCKER_APPROVED_INVOICES: {"status": "approved", "approved_by": "Pat"},
    workspace_closure.BLOCKER_APPLIED_CREDIT_MEMOS: {"status": "applied"},
    workspace_closure.BLOCKER_APPROVED_EXPENSE_REPORTS: {"status": "approved"},
    workspace_closure.BLOCKER_ACTIVE_CONTRACTS: {"status": "active"},
    workspace_closure.BLOCKER_ACCEPTED_DISCOUNTS: {"status": "accepted"},
    workspace_closure.BLOCKER_APPROVED_SUPPLIER_CHANGES: {"status": "approved"},
    workspace_closure.BLOCKER_APPROVED_PREAPPROVALS: {"status": "approved"},
    workspace_closure.BLOCKER_PROGRESSED_PURCHASE_ORDERS: {"status": "received"},
    workspace_closure.BLOCKER_APPROVED_REQUISITIONS: {"status": "approved"},
    workspace_closure.BLOCKER_APPROVED_INTAKE_REQUESTS: {"status": "approved"},
}


async def _preflight(realdb, key: str = "a", role: str = "admin"):
    async with realdb.client(key=key, role=role) as c:
        return await c.get("/api/organization/close")


@pytest.mark.parametrize("check", workspace_closure.TENANT_CHECKS, ids=lambda c: c.code)
async def test_each_tenant_record_class_blocks(realdb, check):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        await _insert_row(
            s, check.table, realdb.info("a").org_id, **BLOCKING_OVERRIDES.get(check.code, {})
        )
        await s.commit()

    resp = await _preflight(realdb)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["closable"] is False
    assert check.code in body["blockers"]
    assert body["slug"] == realdb.info("a").slug


async def test_an_unapproved_trial_does_not_block(realdb):
    """What an abandoned trial plausibly holds: draft and in-review invoices,
    one whose AI read failed, an open credit memo, a draft expense report and
    contract, a discount still on offer, a supplier change awaiting review,
    vendors and purchase orders (typically pulled from an ERP)."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        for status in ("new", "pending", "ready_for_review", "rejected", "failed"):
            await _insert_row(s, "invoices", org_id, status=status)
        await _insert_row(s, "credit_memos", org_id, status="open")
        await _insert_row(s, "expense_reports", org_id, status="draft")
        await _insert_row(s, "contracts", org_id, status="draft")
        await _insert_row(s, "discount_offers", org_id, status="offered")
        await _insert_row(s, "vendor_change_requests", org_id, status="pending")
        await _insert_row(s, "purchase_orders", org_id, status="open")
        await _insert_row(s, "purchase_requisitions", org_id, status="draft")
        await _insert_row(s, "intake_requests", org_id, status="open")
        await _insert_row(s, "expense_preapprovals", org_id, status="pending")
        await s.commit()

    body = (await _preflight(realdb)).json()
    tenant_codes = {c.code for c in workspace_closure.TENANT_CHECKS}
    assert not tenant_codes & set(body["blockers"])


async def test_an_erp_failed_approved_invoice_still_blocks(realdb):
    """`failed` is shared with extraction failures, so the status alone can't
    say; `approved_by` does."""
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        await _insert_row(
            s, "invoices", realdb.info("a").org_id, status="failed", approved_by="Pat"
        )
        await s.commit()
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_APPROVED_INVOICES in body["blockers"]


async def test_other_people_block(realdb):
    # The harness tenant has a user per role, so the admin is not alone.
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_OTHER_USERS in body["blockers"]


async def test_a_paid_subscription_blocks(realdb):
    await realdb.subscribe("a", "scale")
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_PAID_SUBSCRIPTION in body["blockers"]


async def test_the_free_plan_does_not_block(realdb):
    await realdb.subscribe("a", "free")
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_PAID_SUBSCRIPTION not in body["blockers"]


async def test_a_partner_and_its_child_both_block(realdb):
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == realdb.info("b").org_id)
            .values(parent_org_id=realdb.info("a").org_id)
        )
        await s.commit()
    parent = (await _preflight(realdb, "a")).json()
    child = (await _preflight(realdb, "b")).json()
    assert workspace_closure.BLOCKER_PARTNER_CHILDREN in parent["blockers"]
    assert workspace_closure.BLOCKER_PARTNER_MANAGED in child["blockers"]


async def test_a_registered_custom_domain_blocks(realdb):
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == realdb.info("a").org_id)
            .values(settings={"brand": {"custom_domains": ["ap.example.test"]}})
        )
        await s.commit()
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_CUSTOM_DOMAIN in body["blockers"]


@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
async def test_only_an_admin_may_ask_or_close(realdb, role):
    assert (await _preflight(realdb, role=role)).status_code == 403
    async with realdb.client(key="a", role=role) as c:
        resp = await c.post("/api/organization/close", json={"confirm_slug": realdb.info("a").slug})
    assert resp.status_code == 403


async def test_no_session_no_answer(realdb):
    async with realdb.client(key="a", role=None) as c:
        assert (await c.get("/api/organization/close")).status_code == 401
        resp = await c.post("/api/organization/close", json={"confirm_slug": realdb.info("a").slug})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# The closure, on a throwaway tenant
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def trial(realdb):
    """A workspace as self-service signup leaves it — one passwordless admin,
    the free plan — registered on the harness as tenant ``t``. Whatever a test
    leaves of it is deleted afterwards."""
    slug = "close" + uuid.uuid4().hex[:8]
    email = f"owner.{uuid.uuid4().hex[:8]}@example.test"
    result = await provision_tenant(
        company_name=f"Closing {slug}",
        slug=slug,
        admin_email=email,
        admin_name="Trial Owner",
        admin_password=None,
        must_change_password=False,
    )
    realdb.tenants = {
        **realdb.tenants,
        "t": TenantInfo(
            slug=slug,
            db_name=result.db_name,
            org_id=result.organization_id,
            users={"admin": result.user_id},
        ),
    }
    swept = AsyncMock(return_value=0)
    with patch.object(tenant_deletion.storage, "delete_prefix", swept):
        yield {"slug": slug, "email": email, "result": result, "swept": swept}

        try:
            await tenant_deletion.delete_tenant(slug)
        except tenant_deletion.TenantNotFound:
            pass
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            delete(WorkspaceClosure).where(
                WorkspaceClosure.organization_id == result.organization_id
            )
        )
        await s.commit()


async def _close(realdb, slug: str, **extra):
    async with realdb.client(key="t", role="admin") as c:
        return await c.post("/api/organization/close", json={"confirm_slug": slug, **extra})


async def _org_exists(realdb, org_id) -> bool:
    async with realdb.control_sessionmaker()() as s:
        return (await s.get(Organization, org_id)) is not None


async def _database_exists(db_name: str) -> bool:
    return await workspace_closure._database_exists(db_name)


async def _closure_row(realdb, org_id) -> WorkspaceClosure | None:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(
                select(WorkspaceClosure).where(WorkspaceClosure.organization_id == org_id)
            )
        ).scalar_one_or_none()


async def _tenant_audit(db_name: str, action: str) -> list[AuditLog]:
    engine = create_async_engine(_make_tenant_url(db_name), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(select(AuditLog).where(AuditLog.action == action))
            return list(rows.all())
    finally:
        await engine.dispose()


async def test_closing_an_abandoned_trial_releases_its_address(realdb, trial):
    """The dead end, end to end: the address is refused elsewhere while the
    abandoned workspace exists, and accepted once its admin has closed it."""
    body = {"email": trial["email"], "full_name": "New Hire", "role_names": ["ap_clerk"]}
    async with realdb.client(key="b", role="admin") as c:
        refused = await c.post("/api/admin/users", json=body)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "email_in_other_workspace"

    async with realdb.client(key="t", role="admin") as c:
        preflight = await c.get("/api/organization/close")
    assert preflight.json() == {
        "closable": True,
        "blockers": [],
        "slug": trial["slug"],
        "will_delete": {},
    }

    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"closed": True}

    org_id = trial["result"].organization_id
    assert not await _org_exists(realdb, org_id)
    assert not await _database_exists(trial["result"].db_name)
    # The database first, then two document passes (decisions §294).
    assert [c.args for c in trial["swept"].await_args_list] == [(f"{org_id}/",)] * 2
    async with realdb.control_sessionmaker()() as s:
        left = (await s.execute(select(User.id).where(User.organization_id == org_id))).all()
    assert left == []

    # The record that outlives the tenant: who, with what proof, what went.
    row = await _closure_row(realdb, org_id)
    assert row.status == CLOSURE_COMPLETED
    assert row.slug == trial["slug"]
    assert row.requested_by_user_id == trial["result"].user_id
    assert row.step_up_proof == "mfa_off_local"
    assert row.requested_by_roles == ["admin"]
    assert row.database_dropped is True
    assert row.control_rows_removed > 0
    assert row.backups_cleared_at is None  # the operator's leg, still owed

    async with realdb.client(key="b", role="admin") as c:
        accepted = await c.post("/api/admin/users", json=body)
    assert accepted.status_code == 201, accepted.text
    async with realdb.client(key="b", role="admin") as c:
        await c.delete(f"/api/admin/users/{accepted.json()['id']}")


async def test_the_closed_workspaces_token_is_dead(realdb, trial):
    async with realdb.client(key="t", role="admin") as c:
        assert (
            await c.post("/api/organization/close", json={"confirm_slug": trial["slug"]})
        ).status_code == 200
        after = await c.get("/api/auth/me")
    assert after.status_code == 401


async def test_a_mistyped_confirmation_changes_nothing(realdb, trial):
    resp = await _close(realdb, trial["slug"] + "x")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "workspace_close_confirmation_mismatch"
    assert await _org_exists(realdb, trial["result"].organization_id)
    trial["swept"].assert_not_awaited()


async def test_a_record_written_after_the_preflight_still_refuses(realdb, trial):
    """The closure re-checks under its own locks; it does not trust the page."""
    mk = realdb.sessionmaker("t")
    async with mk() as s:
        await _insert_row(
            s, "invoices", trial["result"].organization_id, status="approved", approved_by="Pat"
        )
        await s.commit()

    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "workspace_close_refused"
    assert detail["params"]["blockers"] == [workspace_closure.BLOCKER_APPROVED_INVOICES]
    assert await _org_exists(realdb, trial["result"].organization_id)
    assert await _database_exists(trial["result"].db_name)
    assert await _closure_row(realdb, trial["result"].organization_id) is None
    trial["swept"].assert_not_awaited()

    refused = await _tenant_audit(trial["result"].db_name, "organization.close.refused")
    assert len(refused) == 1
    assert refused[0].details["blockers"] == [workspace_closure.BLOCKER_APPROVED_INVOICES]
    assert "ip" in refused[0].details


async def test_a_second_user_refuses_the_closure(realdb, trial):
    async with realdb.client(key="t", role="admin") as c:
        made = await c.post(
            "/api/admin/users",
            json={
                "email": f"colleague.{uuid.uuid4().hex[:8]}@example.test",
                "full_name": "Colleague",
                "role_names": ["ap_clerk"],
            },
        )
        assert made.status_code == 201, made.text
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["params"]["blockers"] == [workspace_closure.BLOCKER_OTHER_USERS]
    assert await _org_exists(realdb, trial["result"].organization_id)


async def test_a_closure_already_running_is_refused(realdb, trial):
    org_id = trial["result"].organization_id
    async with realdb.control_sessionmaker()() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": workspace_closure._advisory_key(org_id)},
        )
        resp = await _close(realdb, trial["slug"])
        await holder.rollback()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "workspace_close_in_progress"
    assert await _org_exists(realdb, org_id)
    # Audited into the workspace's own log, which still exists.
    rows = await _tenant_audit(trial["result"].db_name, "organization.close.in_progress")
    assert len(rows) == 1


async def test_an_unshippable_audit_trail_deletes_nothing(realdb, trial, monkeypatch):
    """Where audit shipping is on, the trail goes to the archive first; if it
    cannot, the tenant — and every unshipped row in it — stays."""
    monkeypatch.setattr(workspace_closure.settings, "audit_shipping_enabled", True)
    monkeypatch.setattr(
        workspace_closure.audit_log_shipper,
        "flush_tenant",
        AsyncMock(side_effect=RuntimeError("sink down")),
    )
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "workspace_close_audit_unavailable"
    assert await _org_exists(realdb, trial["result"].organization_id)
    assert await _database_exists(trial["result"].db_name)
    assert await _closure_row(realdb, trial["result"].organization_id) is None
    trial["swept"].assert_not_awaited()


def _capture_archive(monkeypatch) -> list:
    """Turn audit shipping on with one in-memory sink; returns what it received."""
    from tests.test_audit_log_shipper import _CapturingAdapter

    sink = _CapturingAdapter()
    monkeypatch.setattr(workspace_closure.settings, "audit_shipping_enabled", True)
    monkeypatch.setattr(workspace_closure.audit_log_shipper, "_build_adapters", lambda: [sink])
    return sink


async def test_the_trail_is_shipped_and_sealed_before_the_tenant_is_dropped(
    realdb, trial, monkeypatch
):
    sink = _capture_archive(monkeypatch)
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    shipped = [row for batch in sink.batches for row in batch]
    closing = [row for row in shipped if row.action == "organization.closing"]
    assert len(closing) == 1
    # The source IP and the role snapshot ride the shipped row, like every
    # other audit row; the control-plane record carries neither IP nor name.
    assert set(closing[0].details) == {"step_up_proof", "ip", "roles"}
    assert closing[0].details["roles"] == ["admin"]


async def test_a_row_committed_after_the_flush_is_shipped_before_the_drop(
    realdb, trial, monkeypatch
):
    """The window between the last flush and the drop is closed: a row that
    lands after a flush is seen under the seal, the seal is released, and the
    next round ships it."""
    sink = _capture_archive(monkeypatch)
    real_flush = workspace_closure.audit_log_shipper.flush_tenant
    rounds = 0

    async def _flush_then_a_late_row(db_name):
        nonlocal rounds
        rounds += 1
        shipped = await real_flush(db_name)
        if rounds == 1:
            from app.services.audit_dispatch import record_auth_audit_or_raise

            await record_auth_audit_or_raise(
                organization_id=trial["result"].organization_id,
                actor_id=None,
                action="auth.login.failure",
                details={"reason": "late"},
            )
        return shipped

    monkeypatch.setattr(workspace_closure.audit_log_shipper, "flush_tenant", _flush_then_a_late_row)
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert rounds == 2
    shipped = [row for batch in sink.batches for row in batch]
    assert any(row.action == "auth.login.failure" for row in shipped)


async def test_a_trail_that_never_stops_growing_deletes_nothing(realdb, trial, monkeypatch):
    _capture_archive(monkeypatch)
    real_flush = workspace_closure.audit_log_shipper.flush_tenant

    async def _flush_then_another(db_name):
        shipped = await real_flush(db_name)
        from app.services.audit_dispatch import record_auth_audit_or_raise

        await record_auth_audit_or_raise(
            organization_id=trial["result"].organization_id,
            actor_id=None,
            action="auth.login.failure",
            details={"reason": "busy"},
        )
        return shipped

    monkeypatch.setattr(workspace_closure.audit_log_shipper, "flush_tenant", _flush_then_another)
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "workspace_close_audit_unavailable"
    assert await _database_exists(trial["result"].db_name)
    assert await _closure_row(realdb, trial["result"].organization_id) is None
    trial["swept"].assert_not_awaited()


async def test_a_half_finished_closure_is_finished_by_a_retry(realdb, trial):
    """The database went, the control-plane rows did not: a retry must finish
    the job rather than refuse on a database it can no longer read."""
    org_id = trial["result"].organization_id
    await workspace_closure._record_started(
        org_id, trial["slug"], trial["result"].user_id, "mfa_off_local", ["admin"]
    )
    from app.services.tenant_provisioning import _drop_postgres_database

    await _drop_postgres_database(trial["result"].db_name)

    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert not await _org_exists(realdb, org_id)
    row = await _closure_row(realdb, org_id)
    assert row.status == CLOSURE_COMPLETED
    assert row.database_dropped is False


async def test_with_mfa_on_a_second_factor_is_required(realdb, trial, monkeypatch):
    monkeypatch.setattr("app.api.auth.settings.mfa_enabled", True)

    # No factor enrolled: refused, never exempted, nothing deleted.
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "sensitive_step_up_no_factor"
    assert await _org_exists(realdb, trial["result"].organization_id)

    secret = pyotp.random_base32()
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(User)
            .where(User.id == trial["result"].user_id)
            .values(mfa_enabled=True, mfa_secret=secret)
        )
        await s.commit()

    # A factor, but no proof offered: the page's cue to ask for one.
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "sensitive_step_up_required"

    resp = await _close(realdb, trial["slug"], step_up={"code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 200, resp.text
    row = await _closure_row(realdb, trial["result"].organization_id)
    assert row.step_up_proof == "totp"


async def test_the_operator_queue_lists_and_marks_owed_backups(realdb, trial):
    assert (await _close(realdb, trial["slug"])).status_code == 200
    owed = await workspace_closure.closures_owing_backups()
    assert trial["slug"] in {r.slug for r in owed}

    org_id = trial["result"].organization_id
    assert await workspace_closure.mark_backups_cleared(org_id, operator="ops-jh") == 1
    assert await workspace_closure.mark_backups_cleared(org_id, operator="ops-jh") == 0
    owed = await workspace_closure.closures_owing_backups()
    assert trial["slug"] not in {r.slug for r in owed}
    row = await _closure_row(realdb, org_id)
    assert row.backups_cleared_by == "ops-jh"
    assert row.backups_cleared_at is not None
    with pytest.raises(ValueError):
        await workspace_closure.mark_backups_cleared(org_id, operator="  ")


async def test_a_started_closure_is_not_marked_cleared(realdb, trial):
    await workspace_closure._record_started(
        trial["result"].organization_id, trial["slug"], trial["result"].user_id, "totp", ["admin"]
    )
    assert (
        await workspace_closure.mark_backups_cleared(
            trial["result"].organization_id, operator="ops-jh"
        )
        == 0
    )
    row = await _closure_row(realdb, trial["result"].organization_id)
    assert row.status == CLOSURE_STARTED
    # Still a live workspace: nothing is owed yet.
    owed = await workspace_closure.closures_owing_backups()
    assert trial["slug"] not in {r.slug for r in owed}


async def test_a_started_closure_whose_organisation_is_gone_is_still_owed(realdb, trial):
    """Completion commits with the control-plane delete, so this should not
    happen; if it ever does, the backups are still owed and must be listed."""
    org_id = trial["result"].organization_id
    await workspace_closure._record_started(
        org_id, trial["slug"], trial["result"].user_id, "totp", ["admin"]
    )
    await tenant_deletion.delete_tenant(trial["slug"])
    owed = await workspace_closure.closures_owing_backups()
    assert org_id in {r.organization_id for r in owed}
    assert await workspace_closure.mark_backups_cleared(org_id, operator="ops-jh") == 1


async def test_a_closed_slug_is_held_until_backups_go_and_the_cooldown_passes(
    realdb, trial, monkeypatch
):
    """A new workspace under the same slug would reuse `feoh_<slug>` (its dumps
    deleted with the closed one's), and would inherit bookmarks, IdP redirect
    URIs and invite emails that still name it."""
    from app.utils import slug as slug_mod

    assert (await _close(realdb, trial["slug"])).status_code == 200
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(slug_mod.SlugHeld):
            await slug_mod.ensure_slug_available(trial["slug"], s)

    # Backups gone, but well inside the cooldown: still held.
    await workspace_closure.mark_backups_cleared(trial["result"].organization_id, operator="ops-jh")
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(slug_mod.SlugHeld):
            await slug_mod.ensure_slug_available(trial["slug"], s)

    # Past the cooldown: free.
    monkeypatch.setattr(slug_mod, "CLOSED_SLUG_COOLDOWN", timedelta(0))
    async with realdb.control_sessionmaker()() as s:
        await slug_mod.ensure_slug_available(trial["slug"], s)


async def test_the_cooldown_is_at_least_the_dpa_backup_window():
    from app.utils.slug import CLOSED_SLUG_COOLDOWN

    assert CLOSED_SLUG_COOLDOWN >= timedelta(days=60)


async def test_the_operator_cli_cannot_provision_a_held_slug(realdb, trial):
    """`provision_tenant` holds the slug itself, so the operator path —
    `scripts/create_tenant.py`, and `deploy/add-tenant.sh` behind it — is held
    exactly like signup."""
    import importlib.util

    assert (await _close(realdb, trial["slug"])).status_code == 200
    path = Path(__file__).resolve().parent.parent / "scripts" / "create_tenant.py"
    spec = importlib.util.spec_from_file_location("_create_tenant_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    from app.utils.slug import SlugHeld

    with pytest.raises(SlugHeld):
        await cli.provision_tenant(
            company_name="Squatter",
            slug=trial["slug"],
            admin_email=f"squat.{uuid.uuid4().hex[:8]}@example.test",
            admin_name="Squatter",
            admin_password="Sup3rSecret!pw",
        )
    assert not await _database_exists(trial["result"].db_name)


def _load_cli():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "workspace_closures.py"
    spec = importlib.util.spec_from_file_location("_workspace_closures_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_says_when_nothing_was_marked(monkeypatch, capsys):
    cli = _load_cli()
    monkeypatch.setattr(cli, "mark_backups_cleared", AsyncMock(return_value=0))
    org_id = uuid.uuid4()
    assert (
        cli.main(["mark-cleared", str(org_id), "--operator", "ops-jh"]) == cli.EXIT_NOTHING_MARKED
    )
    assert "nothing marked" in capsys.readouterr().out
    cli.mark_backups_cleared.assert_awaited_once_with(org_id, operator="ops-jh")

    monkeypatch.setattr(cli, "mark_backups_cleared", AsyncMock(return_value=1))
    assert cli.main(["mark-cleared", str(org_id), "--operator", "ops-jh"]) == 0


def test_the_cli_requires_an_operator():
    cli = _load_cli()
    with pytest.raises(SystemExit):
        cli.main(["mark-cleared", str(uuid.uuid4())])
    with pytest.raises(SystemExit):
        cli.main(["mark-cleared", str(uuid.uuid4()), "--operator", " "])


def test_the_cli_takes_an_organisation_id_not_a_slug():
    cli = _load_cli()
    with pytest.raises(SystemExit):
        cli.main(["mark-cleared", "some-slug", "--operator", "ops-jh"])


def test_the_cli_lists_owed_closures(monkeypatch, capsys):
    cli = _load_cli()
    row = WorkspaceClosure(
        slug="gone-trial",
        organization_id=uuid.uuid4(),
        status=CLOSURE_COMPLETED,
        completed_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    stuck = WorkspaceClosure(
        slug="stuck-trial",
        organization_id=uuid.uuid4(),
        status=CLOSURE_STARTED,
        started_at=datetime(2026, 10, 19, tzinfo=UTC),
        last_failure="RuntimeError",
    )
    monkeypatch.setattr(cli, "closures_owing_backups", AsyncMock(return_value=[row]))
    monkeypatch.setattr(cli, "unfinished_closures", AsyncMock(return_value=[stuck]))
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "gone-trial" in out and "2026-10-20" in out
    # An unfinished closure is surfaced to the operator, with its failure.
    assert "stuck-trial" in out and "RuntimeError" in out


async def test_guarded_tables_stay_locked_until_the_database_is_dropped(realdb, trial, monkeypatch):
    """No write to a guarded table can land between the check and the drop: at
    the moment of the drop, an attempt to record an approval waits on the
    closure's lock."""
    outcomes: list[str] = []
    db_name = trial["result"].db_name
    org_id = trial["result"].organization_id
    real_drop = tenant_deletion._drop_postgres_database

    async def _race_then_drop(name):
        engine = create_async_engine(_make_tenant_url(db_name), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                try:
                    async with conn.begin():
                        await conn.execute(text("SET LOCAL lock_timeout = '300ms'"))
                        await _insert_row(
                            conn, "invoices", org_id, status="approved", approved_by="Pat"
                        )
                    outcomes.append("written")
                except Exception as exc:  # noqa: BLE001 - the lock is the point
                    outcomes.append("lock timeout" if "lock timeout" in str(exc) else repr(exc))
        finally:
            await engine.dispose()
        await real_drop(name)

    monkeypatch.setattr(tenant_deletion, "_drop_postgres_database", _race_then_drop)
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert outcomes == ["lock timeout"]
    assert not await _database_exists(db_name)


async def test_no_user_can_be_added_while_the_closure_runs(realdb, trial):
    """The control-plane checks hold too: the organisation row is locked from the
    check to the delete, so a user inserted mid-closure (an invite, a SCIM or
    SSO provision) waits instead of being deleted unseen."""
    outcomes: list[str] = []
    org_id = trial["result"].organization_id

    async def _sweep_while_inviting(prefix):
        if outcomes:  # the sweep makes two passes; race once
            return 0
        async with realdb.control_sessionmaker()() as s:
            try:
                await s.execute(text("SET LOCAL lock_timeout = '300ms'"))
                s.add(
                    User(
                        id=uuid.uuid4(),
                        email=f"late.{uuid.uuid4().hex[:8]}@example.test",
                        full_name="Late Invite",
                        organization_id=org_id,
                        is_active=True,
                        must_change_password=False,
                    )
                )
                await s.commit()
                outcomes.append("written")
            except Exception as exc:  # noqa: BLE001 - the lock is the point
                await s.rollback()
                outcomes.append("lock timeout" if "lock timeout" in str(exc) else repr(exc))
        return 0

    trial["swept"].side_effect = _sweep_while_inviting
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert outcomes == ["lock timeout"]


@pytest.mark.parametrize(
    "statement",
    [
        # A billing webhook moving the free subscription onto a provider-backed
        # one — an UPDATE, which the org-row lock alone does not hold off.
        "UPDATE subscriptions SET external_subscription_id = 'sub_late' "
        "WHERE organization_id = :org",
        "UPDATE users SET is_active = true WHERE organization_id = :org",
    ],
    ids=["subscription", "user"],
)
async def test_an_existing_row_cannot_be_changed_while_the_closure_runs(realdb, trial, statement):
    outcomes: list[str] = []
    org_id = trial["result"].organization_id

    async def _sweep_while_updating(prefix):
        if outcomes:  # the sweep makes two passes; race once
            return 0
        async with realdb.control_sessionmaker()() as s:
            try:
                await s.execute(text("SET LOCAL lock_timeout = '300ms'"))
                await s.execute(text(statement), {"org": org_id})
                await s.commit()
                outcomes.append("written")
            except Exception as exc:  # noqa: BLE001 - the lock is the point
                await s.rollback()
                outcomes.append("lock timeout" if "lock timeout" in str(exc) else repr(exc))
        return 0

    trial["swept"].side_effect = _sweep_while_updating
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert outcomes == ["lock timeout"]


async def test_a_token_without_a_workspace_claim_cannot_close_another_workspace(realdb, trial):
    """`get_tenant` skips a token with no `org` claim; the route checks the
    account's own workspace itself."""
    from jose import jwt

    from app.api.deps import ALGORITHM
    from app.config import settings as app_settings

    token = jwt.encode(
        {
            "sub": str(realdb.info("a").users["admin"]),
            "typ": "user",
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        app_settings.secret_key,
        algorithm=ALGORITHM,
    )
    async with realdb.client(key="t", role=None) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        pre = await c.get("/api/organization/close")
        resp = await c.post("/api/organization/close", json={"confirm_slug": trial["slug"]})
    assert pre.status_code == 403
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "token_tenant_mismatch"
    assert await _org_exists(realdb, trial["result"].organization_id)


# ---------------------------------------------------------------------------
# What the deployment must be able to promise (items: MFA, the archive, SQS)
# ---------------------------------------------------------------------------


def _deployed(monkeypatch, **overrides):
    monkeypatch.setattr(workspace_closure.settings, "environment", "production")
    for key, value in overrides.items():
        monkeypatch.setattr(workspace_closure.settings, key, value)


async def test_deployed_without_mfa_the_preflight_says_so(realdb, monkeypatch):
    """`require_sensitive_step_up` refuses every call there, so the page must
    not offer a close it cannot complete."""
    _deployed(
        monkeypatch,
        mfa_enabled=False,
        audit_shipping_enabled=True,
        audit_shipping_providers="cloudwatch",
    )
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_SECOND_FACTOR_UNAVAILABLE in body["blockers"]
    assert workspace_closure.BLOCKER_AUDIT_ARCHIVE_UNAVAILABLE not in body["blockers"]


@pytest.mark.parametrize(
    ("enabled", "providers"),
    [(False, "cloudwatch"), (True, ""), (True, "mock"), (True, "cloudwatch,mock")],
    ids=["shipping-off", "no-sink", "mock-only", "mock-in-the-mix"],
)
async def test_deployed_without_a_real_archive_the_preflight_says_so(
    realdb, monkeypatch, enabled, providers
):
    _deployed(
        monkeypatch,
        mfa_enabled=True,
        audit_shipping_enabled=enabled,
        audit_shipping_providers=providers,
    )
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_AUDIT_ARCHIVE_UNAVAILABLE in body["blockers"]
    assert workspace_closure.BLOCKER_SECOND_FACTOR_UNAVAILABLE not in body["blockers"]


async def test_deployed_in_lambda_audit_mode_the_preflight_says_so(realdb, monkeypatch):
    _deployed(
        monkeypatch,
        mfa_enabled=True,
        audit_shipping_enabled=True,
        audit_shipping_providers="cloudwatch",
        audit_mode="lambda",
    )
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_AUDIT_QUEUE_UNVERIFIABLE in body["blockers"]


async def test_local_dev_needs_neither_mfa_nor_an_archive(realdb):
    body = (await _preflight(realdb)).json()
    assert not set(workspace_closure.DEPLOYMENT_BLOCKERS) & set(body["blockers"])


async def test_deployed_without_a_real_archive_the_closure_is_refused(realdb, trial, monkeypatch):
    """Refused at close time too, under the lock — never only on the page."""
    _deployed(monkeypatch, mfa_enabled=True, audit_shipping_enabled=False)
    with pytest.raises(workspace_closure.ClosureRefused) as refused:
        await workspace_closure.close_workspace(
            org_id=trial["result"].organization_id,
            actor_id=trial["result"].user_id,
            step_up_proof="totp",
        )
    assert refused.value.blockers == [workspace_closure.BLOCKER_AUDIT_ARCHIVE_UNAVAILABLE]
    assert await _database_exists(trial["result"].db_name)
    trial["swept"].assert_not_awaited()


# ---------------------------------------------------------------------------
# The actor, re-validated under the lock
# ---------------------------------------------------------------------------


async def test_an_actor_who_is_no_longer_an_admin_is_refused_under_the_lock(realdb, trial):
    from app.models.user import Role, UserRole

    org_id = trial["result"].organization_id
    with pytest.raises(workspace_closure.ClosureActorInvalid):
        await workspace_closure.close_workspace(
            org_id=org_id, actor_id=uuid.uuid4(), step_up_proof="mfa_off_local"
        )
    async with realdb.control_sessionmaker()() as s:
        admin_role = (
            await s.execute(
                select(Role.id).where(Role.name == "admin", Role.organization_id.is_(None))
            )
        ).scalar_one()
        await s.execute(
            delete(UserRole).where(
                UserRole.user_id == trial["result"].user_id, UserRole.role_id == admin_role
            )
        )
        await s.commit()
    with pytest.raises(workspace_closure.ClosureActorInvalid):
        await workspace_closure.close_workspace(
            org_id=org_id, actor_id=trial["result"].user_id, step_up_proof="mfa_off_local"
        )
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(User).where(User.id == trial["result"].user_id).values(is_active=False)
        )
        await s.commit()
    with pytest.raises(workspace_closure.ClosureActorInvalid):
        await workspace_closure.close_workspace(
            org_id=org_id, actor_id=trial["result"].user_id, step_up_proof="mfa_off_local"
        )
    assert await _database_exists(trial["result"].db_name)


# ---------------------------------------------------------------------------
# A failed drop: the documents stay, the failure is recorded, a retry finishes
# ---------------------------------------------------------------------------


async def test_a_drop_that_fails_leaves_the_documents_and_is_finished_by_a_retry(
    realdb, trial, monkeypatch, caplog
):
    org_id = trial["result"].organization_id
    real_drop = tenant_deletion._drop_postgres_database

    async def _refuse(_name):
        raise RuntimeError("database is being accessed by other users")

    monkeypatch.setattr(tenant_deletion, "_drop_postgres_database", _refuse)
    caplog.set_level("ERROR", logger="app.services.workspace_closure")
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "workspace_close_incomplete"
    # Nothing irreversible happened: the database and every document remain.
    trial["swept"].assert_not_awaited()
    assert await _database_exists(trial["result"].db_name)
    assert await _org_exists(realdb, org_id)
    # Recorded where it survives: the closure row, and an id-only log line.
    row = await _closure_row(realdb, org_id)
    assert row.status == CLOSURE_STARTED
    assert row.last_failure == "RuntimeError"
    assert row.last_failure_at is not None
    assert any(str(org_id) in r.getMessage() for r in caplog.records)
    assert trial["email"] not in caplog.text

    # The admin's operator view surfaces it while it is unfinished.
    assert org_id in {r.organization_id for r in await workspace_closure.unfinished_closures()}

    monkeypatch.setattr(tenant_deletion, "_drop_postgres_database", real_drop)
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    row = await _closure_row(realdb, org_id)
    assert row.status == CLOSURE_COMPLETED
    assert row.last_failure is None


# ---------------------------------------------------------------------------
# What a closure would delete, shown before it is asked for
# ---------------------------------------------------------------------------


async def test_the_preflight_counts_what_will_be_deleted(realdb, trial):
    org_id = trial["result"].organization_id
    mk = realdb.sessionmaker("t")
    async with mk() as s:
        for _ in range(2):
            await _insert_row(s, "invoices", org_id, status="new")
        await _insert_row(s, "credit_memos", org_id, status="open")
        await s.commit()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            User(
                id=uuid.uuid4(),
                email=f"former.{uuid.uuid4().hex[:8]}@example.test",
                full_name="Former Colleague",
                organization_id=org_id,
                is_active=False,
                must_change_password=False,
            )
        )
        await s.commit()
    async with realdb.client(key="t", role="admin") as c:
        body = (await c.get("/api/organization/close")).json()
    assert body["closable"] is True
    # credit_memos' FK made a vendor too; an inactive user does not block.
    assert body["will_delete"] == {
        "invoices": 2,
        "credit_memos": 1,
        "vendors": 1,
        "inactive_users": 1,
    }


# ---------------------------------------------------------------------------
# A contract that was in force and then cancelled
# ---------------------------------------------------------------------------


async def test_a_contract_activated_then_cancelled_still_blocks(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        vendor_id = await _insert_row(s, "vendors", org_id)
        await s.commit()
    async with realdb.client(key="a", role="admin") as c:
        made = await c.post(
            "/api/contracts",
            json={"contract_number": f"C-{uuid.uuid4().hex[:6]}", "vendor_id": str(vendor_id)},
        )
        assert made.status_code in (200, 201), made.text
        cid = made.json()["id"]
        assert (await c.post(f"/api/contracts/{cid}/activate")).status_code == 200
        assert (await c.post(f"/api/contracts/{cid}/cancel")).status_code == 200
    async with mk() as s:
        status, activated_at = (
            await s.execute(
                text("SELECT status, activated_at FROM contracts WHERE id = :id"), {"id": cid}
            )
        ).one()
    assert str(status).endswith("cancelled")
    assert activated_at is not None
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_ACTIVE_CONTRACTS in body["blockers"]


async def test_a_contract_cancelled_as_a_draft_does_not_block(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        await _insert_row(s, "contracts", org_id, status="cancelled")
        await s.commit()
    body = (await _preflight(realdb)).json()
    assert workspace_closure.BLOCKER_ACTIVE_CONTRACTS not in body["blockers"]


# ---------------------------------------------------------------------------
# Redis: nothing a deleted account kept stays behind
# ---------------------------------------------------------------------------


class _ScanningRedis:
    """FakeRedis plus the key/value + SCAN surface the cleanup uses."""

    def __init__(self):
        from tests.test_session_management import FakeRedis

        self.inner = FakeRedis()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def get(self, key):
        return self.inner.strings.get(key)

    async def getdel(self, key):
        return self.inner.strings.pop(key, None)

    async def scan_iter(self, match: str):
        import fnmatch

        keys = list(self.inner.strings) + list(self.inner.zsets) + list(self.inner.hashes)
        for key in keys:
            if fnmatch.fnmatchcase(key, match):
                yield key


async def test_a_closure_clears_the_deleted_accounts_redis_keys(realdb, trial, monkeypatch):
    from app.redis import BLOCKLIST_PREFIX, _session_key, _session_meta_key
    from app.services import password_reset
    from app.services.mfa import PENDING_ENROLL_PREFIX

    fake = _ScanningRedis()

    async def _get():
        return fake

    for target in (
        "app.redis.get_redis",
        "app.services.credential_cleanup.get_redis",
        "app.services.password_reset.get_redis",
    ):
        monkeypatch.setattr(target, _get)

    uid = trial["result"].user_id
    await fake.zadd(_session_key(uid), {"jti-old": 1.0})
    await fake.hset(_session_meta_key(uid), "jti-old", "{}")
    await fake.setex(f"{PENDING_ENROLL_PREFIX}{uid}", 900, "secret")
    reset_token = await password_reset.issue_reset_token(uid)
    other = await password_reset.issue_reset_token(uuid.uuid4())

    assert (await _close(realdb, trial["slug"])).status_code == 200

    assert _session_key(uid) not in fake.inner.zsets
    assert _session_meta_key(uid) not in fake.inner.hashes
    assert f"{PENDING_ENROLL_PREFIX}{uid}" not in fake.inner.strings
    assert await password_reset.peek_reset_token(reset_token) is None
    # Someone else's link is untouched.
    assert await password_reset.peek_reset_token(other) is not None
    # And the session is refused by the blocklist, not only by the missing row.
    assert f"{BLOCKLIST_PREFIX}jti-old" in fake.inner.strings


# ---------------------------------------------------------------------------
# The sign-in page of a closed workspace
# ---------------------------------------------------------------------------


async def test_the_closed_workspaces_sign_in_page_reads_cleanly(realdb, trial):
    """`/login?reason=workspace_closed` renders its notice from the URL alone;
    the only calls it makes are the SSO / SAML config probes, which it treats
    as optional. Against a really-deleted workspace they must be an ordinary
    refusal it swallows — never a 5xx."""
    assert (await _close(realdb, trial["slug"])).status_code == 200
    async with realdb.client(key="a", role=None) as c:
        oidc = await c.get("/api/auth/sso/config", params={"slug": trial["slug"]})
        saml = await c.get("/api/auth/saml/config", params={"slug": trial["slug"]})
    assert oidc.status_code < 500 and saml.status_code < 500
    assert oidc.status_code == 404 and saml.status_code == 404


# ---------------------------------------------------------------------------
# Signup after a closure
# ---------------------------------------------------------------------------


async def test_the_same_address_can_sign_up_again_after_the_closure(realdb, trial, monkeypatch):
    """The address the closed workspace held goes through /start and /complete
    for a new workspace (under a different name — the old one is resting)."""
    from app.api import signup as signup_mod
    from app.models.signup import EmailVerification

    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: AsyncMock())
    assert (await _close(realdb, trial["slug"])).status_code == 200

    new_slug = "again" + uuid.uuid4().hex[:8]
    async with realdb.client(key="a", role=None) as c:
        started = await c.post(
            "/api/signup/start",
            json={
                "company_name": "Second Try",
                "slug": new_slug,
                "admin_name": "Trial Owner",
                "admin_email": trial["email"],
                "captcha_token": "x",
            },
        )
    assert started.status_code in (200, 202), started.text
    async with realdb.control_sessionmaker()() as s:
        token = (
            await s.execute(
                select(EmailVerification.token).where(EmailVerification.slug == new_slug)
            )
        ).scalar_one()
    try:
        async with realdb.client(key="a", role=None) as c:
            done = await c.post("/api/signup/complete", json={"token": token})
        assert done.status_code == 200, done.text
        assert done.json()["slug"] == new_slug
    finally:
        try:
            await tenant_deletion.delete_tenant(new_slug)
        except tenant_deletion.TenantNotFound:
            pass


async def test_signup_states_a_resting_slug_in_its_own_words(realdb, trial):
    from tests.test_signup import _seed_verification

    assert (await _close(realdb, trial["slug"])).status_code == 200
    token = await _seed_verification(
        realdb, slug=trial["slug"], email=f"new.{uuid.uuid4().hex[:8]}@example.test"
    )
    async with realdb.client(key="a", role=None) as c:
        check = await c.get("/api/signup/slug-check", params={"slug": trial["slug"]})
        done = await c.post("/api/signup/complete", json={"token": token})
    assert check.json()["available"] is False
    assert check.json()["reason_code"] == "signup_slug_held"
    assert done.status_code == 409
    assert done.json()["detail"]["code"] == "signup_slug_held"
    assert not await _database_exists(trial["result"].db_name)


async def test_the_closed_workspaces_own_signup_link_is_the_uniform_410(realdb, trial):
    """The closure removes the verification rows for its slug, so the link
    that created the workspace answers like any dead link — it never re-states
    a workspace that is gone."""
    from tests.test_signup import _seed_verification

    token = await _seed_verification(
        realdb, slug=trial["slug"], email=trial["email"], consumed=True
    )
    assert (await _close(realdb, trial["slug"])).status_code == 200
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 410


# ---------------------------------------------------------------------------
# A slug reused after its cooldown inherits nothing addressed to the old owner
# ---------------------------------------------------------------------------


async def test_a_reused_slug_inherits_no_email_approval_of_the_closed_workspace(
    realdb, trial, monkeypatch
):
    from app.config import settings as app_settings
    from app.services.email_action_token import build_action_token
    from app.utils import slug as slug_mod

    key = "closure-test-signing-key-0123456789abcdef"
    monkeypatch.setattr(app_settings, "email_action_signing_key", key)
    old_token = build_action_token(
        tenant_slug=trial["slug"],
        invoice_id=uuid.uuid4(),
        actor_id=trial["result"].user_id,
        action="approve",
        facts="f" * 16,
        signing_key=key,
        ttl_hours=72,
    )
    assert (await _close(realdb, trial["slug"])).status_code == 200
    await workspace_closure.mark_backups_cleared(trial["result"].organization_id, operator="ops-jh")
    monkeypatch.setattr(slug_mod, "CLOSED_SLUG_COOLDOWN", timedelta(0))
    successor = await provision_tenant(
        company_name="Successor",
        slug=trial["slug"],
        admin_email=f"next.{uuid.uuid4().hex[:8]}@example.test",
        admin_name="Next Owner",
        admin_password="Sup3rSecret!pw",
    )
    assert successor.organization_id != trial["result"].organization_id
    async with realdb.client(key="a", role=None) as c:
        page = await c.get(f"/api/invoices/email-action/{old_token}")
        done = await c.post(f"/api/invoices/email-action/{old_token}/confirm")
    # The token names a user and an invoice that only ever existed in the
    # closed workspace: neither resolves in its successor.
    assert page.status_code == 400
    assert "This link is invalid or has expired" in page.text
    assert "Account unavailable" in done.text
    try:
        await tenant_deletion.delete_tenant(trial["slug"])
    except tenant_deletion.TenantNotFound:
        pass
