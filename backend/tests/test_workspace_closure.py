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
        await _insert_row(s, "purchase_orders", org_id)
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
    assert preflight.json() == {"closable": True, "blockers": [], "slug": trial["slug"]}

    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"closed": True}

    org_id = trial["result"].organization_id
    assert not await _org_exists(realdb, org_id)
    assert not await _database_exists(trial["result"].db_name)
    trial["swept"].assert_awaited_once_with(f"{org_id}/")
    async with realdb.control_sessionmaker()() as s:
        left = (await s.execute(select(User.id).where(User.organization_id == org_id))).all()
    assert left == []

    # The record that outlives the tenant: who, with what proof, what went.
    row = await _closure_row(realdb, org_id)
    assert row.status == CLOSURE_COMPLETED
    assert row.slug == trial["slug"]
    assert row.requested_by_user_id == trial["result"].user_id
    assert row.step_up_proof == "mfa_off_local"
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
    assert refused[0].details == {"blockers": [workspace_closure.BLOCKER_APPROVED_INVOICES]}


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


async def test_the_trail_is_shipped_before_the_tenant_is_dropped(realdb, trial, monkeypatch):
    seen: list[str] = []

    async def _flush(db_name):
        # Called while the database (and the closing row) still exist.
        seen.append(db_name)
        closing = await _tenant_audit(db_name, "organization.closing")
        assert len(closing) == 1
        assert closing[0].details == {"step_up_proof": "mfa_off_local"}
        return 1

    monkeypatch.setattr(workspace_closure.settings, "audit_shipping_enabled", True)
    monkeypatch.setattr(workspace_closure.audit_log_shipper, "flush_tenant", _flush)
    resp = await _close(realdb, trial["slug"])
    assert resp.status_code == 200, resp.text
    assert seen == [trial["result"].db_name]


async def test_a_half_finished_closure_is_finished_by_a_retry(realdb, trial):
    """The database went, the control-plane rows did not: a retry must finish
    the job rather than refuse on a database it can no longer read."""
    org_id = trial["result"].organization_id
    await workspace_closure._record_started(
        org_id, trial["slug"], trial["result"].user_id, "mfa_off_local"
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
    assert await workspace_closure.mark_backups_cleared(org_id) == 1
    assert await workspace_closure.mark_backups_cleared(org_id) == 0
    owed = await workspace_closure.closures_owing_backups()
    assert trial["slug"] not in {r.slug for r in owed}


async def test_a_started_closure_is_not_marked_cleared(realdb, trial):
    await workspace_closure._record_started(
        trial["result"].organization_id, trial["slug"], trial["result"].user_id, "totp"
    )
    assert await workspace_closure.mark_backups_cleared(trial["result"].organization_id) == 0
    row = await _closure_row(realdb, trial["result"].organization_id)
    assert row.status == CLOSURE_STARTED
    # Still a live workspace: nothing is owed yet.
    owed = await workspace_closure.closures_owing_backups()
    assert trial["slug"] not in {r.slug for r in owed}


async def test_a_started_closure_whose_organisation_is_gone_is_still_owed(realdb, trial):
    """Completion commits with the control-plane delete, so this should not
    happen; if it ever does, the backups are still owed and must be listed."""
    org_id = trial["result"].organization_id
    await workspace_closure._record_started(org_id, trial["slug"], trial["result"].user_id, "totp")
    await tenant_deletion.delete_tenant(trial["slug"])
    owed = await workspace_closure.closures_owing_backups()
    assert org_id in {r.organization_id for r in owed}
    assert await workspace_closure.mark_backups_cleared(org_id) == 1


async def test_a_closed_slug_is_held_until_its_backups_are_removed(realdb, trial):
    """A new workspace under the same slug would reuse `feoh_<slug>`, and its
    dumps would be deleted with the closed one's."""
    from app.utils.slug import SlugError, ensure_slug_available

    assert (await _close(realdb, trial["slug"])).status_code == 200
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(SlugError):
            await ensure_slug_available(trial["slug"], s)
    await workspace_closure.mark_backups_cleared(trial["result"].organization_id)
    async with realdb.control_sessionmaker()() as s:
        await ensure_slug_available(trial["slug"], s)


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
    assert cli.main(["mark-cleared", str(org_id)]) == cli.EXIT_NOTHING_MARKED
    assert "nothing marked" in capsys.readouterr().out
    cli.mark_backups_cleared.assert_awaited_once_with(org_id)

    monkeypatch.setattr(cli, "mark_backups_cleared", AsyncMock(return_value=1))
    assert cli.main(["mark-cleared", str(org_id)]) == 0


def test_the_cli_takes_an_organisation_id_not_a_slug():
    cli = _load_cli()
    with pytest.raises(SystemExit):
        cli.main(["mark-cleared", "some-slug"])


def test_the_cli_lists_owed_closures(monkeypatch, capsys):
    cli = _load_cli()
    row = WorkspaceClosure(
        slug="gone-trial",
        organization_id=uuid.uuid4(),
        status=CLOSURE_COMPLETED,
        completed_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    monkeypatch.setattr(cli, "closures_owing_backups", AsyncMock(return_value=[row]))
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "gone-trial" in out and "2026-10-20" in out


async def test_guarded_tables_stay_locked_until_the_database_is_dropped(realdb, trial):
    """No write to a guarded table can land between the check and the drop:
    while the deletion is sweeping documents — after the check, before the
    drop — an attempt to record an approval waits on the closure's lock."""
    outcomes: list[str] = []
    db_name = trial["result"].db_name
    org_id = trial["result"].organization_id

    async def _sweep_while_racing(prefix):
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
        return 0

    trial["swept"].side_effect = _sweep_while_racing
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
