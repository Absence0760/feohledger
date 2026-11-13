"""Tenant deletion — the code behind a published deletion promise.

``/legal/dpa`` § 13 and clause 10 of the Terms commit to destroying a
customer's tenant database, control-plane records and stored documents within
60 days of termination. ``services/tenant_deletion`` is what keeps that, so the
properties worth pinning are the ones whose failure would leave the promise
broken while the operator believed it kept:

* **Completeness.** Every control-plane table is either swept or exempted with
  a reason, held against ``CONTROL_TABLES`` itself — the same drift a register
  has, and the same answer. A new control-plane table cannot be added without
  someone deciding which it is.
* **Ordering.** Documents, then database, then the control-plane row that names
  them. The instinct is the reverse, and the reverse is unrecoverable: the org
  id is the object-storage prefix, and the organisation row is the only place
  it is written down.
* **Blast radius.** System roles are shared by every tenant on the platform and
  a neighbouring org's rows are none of this operation's business. Both are
  asserted against a real database rather than reasoned about.
* **Refusals.** A partner org would silently orphan its children
  (``parent_org_id`` is ON DELETE SET NULL), so it is refused outright.
"""

from __future__ import annotations

import types
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.services import erp_oauth, post_commit, tenant_deletion
from app.services.credential_crypto import CredentialCryptoError
from app.services.erp_adapters.sage_accounting import SAGE_ACCOUNTING_OAUTH
from app.services.tenant_deletion import (
    CONTROL_DELETIONS,
    CONTROL_TABLES_EXEMPT,
    TenantDeletionError,
    delete_tenant,
    format_confirmation,
    plan_tenant_deletion,
)
from app.services.tenant_provisioning import CONTROL_TABLES, provision_tenant

# ---------------------------------------------------------------------------
# Completeness — the sweep against the control-plane table set
# ---------------------------------------------------------------------------


def test_every_control_plane_table_is_swept_or_deliberately_exempt():
    """The gate that stops a new control-plane table surviving a deletion.

    `tenant_deletion` raises this at import too, so a violation cannot reach a
    running process — this test is what names it in CI with an explanation
    rather than a traceback at boot.
    """
    swept = {table for table, _sql in CONTROL_DELETIONS}
    unaccounted = CONTROL_TABLES - swept - set(CONTROL_TABLES_EXEMPT)
    assert unaccounted == set(), (
        "control-plane table(s) a tenant deletion would leave behind: "
        f"{sorted(unaccounted)}. Add a DELETE to CONTROL_DELETIONS, or an entry to "
        "CONTROL_TABLES_EXEMPT saying why the table is not tenant data."
    )


def test_a_table_is_not_both_swept_and_exempt():
    swept = {table for table, _sql in CONTROL_DELETIONS}
    assert swept & set(CONTROL_TABLES_EXEMPT) == set()


def test_the_sweep_does_not_invent_tables_outside_the_control_plane():
    """A DELETE against a tenant-DB table would run on the wrong database."""
    swept = {table for table, _sql in CONTROL_DELETIONS}
    assert swept <= CONTROL_TABLES, f"not control-plane tables: {sorted(swept - CONTROL_TABLES)}"


def test_organizations_is_deleted_last():
    """Every other sweep is scoped by `organization_id`; the org row is what
    the operator can still find the tenant by if one of them fails."""
    assert CONTROL_DELETIONS[-1][0] == "organizations"


def test_system_roles_are_unreachable_by_construction():
    """A system role carries `organization_id IS NULL` and is shared by every
    tenant. `= :org` never matches NULL in SQL, which is the entire safety
    argument — so the query must stay an equality test, not an IS NOT DISTINCT
    FROM or a Python-side filter."""
    roles_sql = dict(CONTROL_DELETIONS)["roles"]
    assert roles_sql == "DELETE FROM roles WHERE organization_id = :org"


def test_each_count_is_derived_from_its_own_delete():
    """The operator's written confirmation is built from these counts. A count
    whose WHERE drifted from the delete's would report a number the deletion
    does not act on."""
    for table, delete_sql in CONTROL_DELETIONS:
        count_sql = str(tenant_deletion._count_sql_for(table))
        assert count_sql == delete_sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)


# ---------------------------------------------------------------------------
# Ordering — documents, then database, then the row that names them
# ---------------------------------------------------------------------------


@dataclass
class _FakePlan:
    organization_id: uuid.UUID
    slug: str
    name: str
    db_name: str
    database_exists: bool
    control_rows: dict

    @property
    def total_control_rows(self) -> int:
        return sum(self.control_rows.values())


@pytest.mark.asyncio
async def test_the_database_goes_first_then_documents_then_the_control_row():
    org_id = uuid.uuid4()
    plan = _FakePlan(
        organization_id=org_id,
        slug="ordertest",
        name="Order Test",
        db_name="feoh_ordertest",
        database_exists=True,
        control_rows={"users": 1},
    )
    order: list[str] = []

    async def _sweep(prefix):
        assert prefix == f"{org_id}/"
        order.append("storage")
        return 4 if order.count("storage") == 1 else 0

    async def _drop(db_name):
        assert db_name == "feoh_ordertest"
        order.append("database")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def begin(self):
            return self

        async def execute(self, *_args, **_kwargs):
            if "control" not in order:
                order.append("control")
            result = AsyncMock()
            result.rowcount = 1
            return result

    async def _grant(_plan, _session):
        order.append("grant")
        return None

    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", _sweep),
        patch.object(tenant_deletion, "_drop_postgres_database", _drop),
        patch.object(tenant_deletion, "_read_erp_grant", _grant),
        patch.object(tenant_deletion, "control_session_factory", lambda: _Session()),
    ):
        result = await delete_tenant("ordertest")

    assert order == ["database", "storage", "storage", "grant", "control"], (
        "the database goes first, so a drop that fails leaves a whole workspace "
        "rather than one whose files are gone (decisions §306); the sweep runs twice "
        "for an object PUT mid-listing; the ERP grant is revoked once the workspace "
        "is unusable but while its sealed tokens can still be read (decisions §371); "
        "and the control-plane row — the only place the object-storage prefix is "
        "written down — goes last (decisions §179)"
    )
    assert result.objects_deleted == 4
    assert result.database_dropped is True


@pytest.mark.asyncio
async def test_a_drop_that_fails_leaves_the_documents_in_place():
    org_id = uuid.uuid4()
    plan = _FakePlan(
        organization_id=org_id,
        slug="dropfail",
        name="Drop Fail",
        db_name="feoh_dropfail",
        database_exists=True,
        control_rows={"users": 1},
    )
    sweep = AsyncMock(return_value=3)

    async def _drop(db_name):
        raise RuntimeError("database is being accessed by other users")

    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", sweep),
        patch.object(tenant_deletion, "_drop_postgres_database", _drop),
        pytest.raises(RuntimeError),
    ):
        await delete_tenant("dropfail")
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_object_put_after_the_first_listing_is_swept_by_the_second():
    """An upload in flight when the database went may PUT its object after the
    first listing; the second pass removes it, and nothing can commit after."""
    org_id = uuid.uuid4()
    plan = _FakePlan(
        organization_id=org_id,
        slug="latepu",
        name="Late Put",
        db_name="feoh_latepu",
        database_exists=False,
        control_rows={},
    )
    bucket = {f"{org_id}/a.pdf"}
    passes = 0

    async def _sweep(prefix):
        nonlocal passes
        passes += 1
        found = {k for k in bucket if k.startswith(prefix)}
        bucket.difference_update(found)
        if passes == 1:
            bucket.add(f"{org_id}/late.pdf")  # PUT lands after the listing
        return len(found)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def begin(self):
            return self

        async def execute(self, *_args, **_kwargs):
            result = AsyncMock()
            result.rowcount = 0
            return result

    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", _sweep),
        patch.object(tenant_deletion, "_read_erp_grant", AsyncMock(return_value=None)),
        patch.object(tenant_deletion, "control_session_factory", lambda: _Session()),
    ):
        result = await delete_tenant("latepu")
    assert bucket == set()
    assert result.objects_deleted == 2


@pytest.mark.asyncio
async def test_dry_run_destroys_nothing():
    plan = _FakePlan(
        organization_id=uuid.uuid4(),
        slug="drytest",
        name="Dry Test",
        db_name="feoh_drytest",
        database_exists=True,
        control_rows={"users": 3},
    )
    sweep = AsyncMock()
    drop = AsyncMock()

    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", sweep),
        patch.object(tenant_deletion, "_drop_postgres_database", drop),
    ):
        result = await delete_tenant("drytest", dry_run=True)

    sweep.assert_not_awaited()
    drop.assert_not_awaited()
    assert result.dry_run is True
    assert result.erp_grant_provider is None
    assert result.objects_deleted == 0
    assert result.database_dropped is False
    assert result.control_rows_deleted == {}


def test_the_written_confirmation_states_what_it_does_not_reach():
    """A confirmation that overstates is worse than none — the DPA discloses
    the residues, so the operator's evidence of deletion must too."""
    plan = _FakePlan(
        organization_id=uuid.uuid4(),
        slug="conf",
        name="Confirm Co",
        db_name="feoh_conf",
        database_exists=True,
        control_rows={},
    )
    text_out = format_confirmation(
        tenant_deletion.TenantDeletionResult(
            plan=plan,
            objects_deleted=7,
            database_dropped=True,
            control_rows_deleted={"users": 2, "organizations": 1},
        )
    )
    assert "Confirm Co" in text_out
    assert "7 object(s)" in text_out
    assert "feoh_conf" in text_out
    assert "backup" in text_out.lower()
    assert "write-once" in text_out.lower()


# ---------------------------------------------------------------------------
# The ERP OAuth grant — revoked best-effort, reported, never blocking (§371)
# ---------------------------------------------------------------------------

_GRANT_TOKENS = {"access_token": "ACCESS-SECRET-9", "refresh_token": "REFRESH-SECRET-9"}


def _grant_settings(provider: str) -> dict:
    return {
        "erp": {
            "type": provider,
            "integration_method": "direct",
            "oauth": {"provider": provider, "client_source": "platform"},
        }
    }


class _GrantSession:
    """A control-plane session: the settings SELECT answers ``org_settings``,
    every DELETE one row. Records whether the control-plane step ran."""

    def __init__(self, org_settings: dict | None):
        self.org_settings = org_settings
        self.deletes = 0
        self.savepoints = 0
        self.info: dict = {}  # where `post_commit` queues its jobs

    def in_nested_transaction(self) -> bool:
        return False

    async def commit(self):
        """The closure's commit, as SQLAlchemy's `after_commit` sees it: the
        queued post-commit jobs start, and the test awaits them."""
        post_commit._on_after_commit(self)
        await post_commit.drain_post_commit()

    def rollback(self):
        post_commit._on_after_rollback(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self

    def begin_nested(self):
        self.savepoints += 1
        return self

    async def execute(self, statement, *_args, **_kwargs):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.org_settings
        if str(statement).lstrip().upper().startswith("DELETE"):
            self.deletes += 1
            result.rowcount = 1
        return result


class _ScriptedRevoke:
    """Stands in for the provider's revoke endpoint (``erp_oauth.httpx``)."""

    def __init__(self, status: int = 200, error: Exception | None = None):
        self.status, self.error = status, error
        self.calls: list[dict] = []

    def install(self, monkeypatch):
        scripted = self

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, headers=None, auth=None, json=None):
                scripted.calls.append({"url": url, "data": dict(data or {}), "auth": auth})
                if scripted.error is not None:
                    raise scripted.error
                response = MagicMock()
                response.status_code = scripted.status
                return response

        monkeypatch.setattr(
            erp_oauth,
            "httpx",
            types.SimpleNamespace(
                AsyncClient=_Client, BasicAuth=httpx.BasicAuth, HTTPError=httpx.HTTPError
            ),
        )
        return self


@pytest.fixture
def sage_platform_app(monkeypatch):
    monkeypatch.setattr(settings, "erp_sage_accounting_client_id", "sage-platform-client")
    monkeypatch.setattr(settings, "erp_sage_accounting_client_secret", "sage-platform-secret")
    monkeypatch.setattr(settings, "erp_sage_accounting_revoke_url", "")


async def _delete_with_grant(monkeypatch, provider: str | None, *, unreadable: bool = False):
    """Run `delete_tenant` over a tenant whose org settings name ``provider``'s
    consent and whose sealed store holds its tokens (None: no grant at all)."""
    org_id = uuid.uuid4()
    plan = _FakePlan(
        organization_id=org_id,
        slug="grant",
        name="Grant Co",
        db_name="feoh_grant",
        database_exists=False,
        control_rows={},
    )
    org_settings = _grant_settings(provider) if provider else {}
    session = _GrantSession(org_settings)

    async def _resolved(got_org_id, got_settings, _db):
        assert got_org_id == org_id
        if unreadable:
            raise CredentialCryptoError("open failed (kms)")
        erp = dict(got_settings.get("erp") or {})
        oauth = dict(erp.pop("oauth", None) or {})
        if provider:
            oauth.update(_GRANT_TOKENS)
        return erp, oauth

    monkeypatch.setattr(erp_oauth, "resolved_erp", _resolved)
    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)),
        patch.object(tenant_deletion, "control_session_factory", lambda: session),
    ):
        result = await delete_tenant("grant")
    return result, session


@pytest.mark.asyncio
async def test_a_connected_erp_grant_is_revoked_before_the_control_plane_goes(
    monkeypatch, sage_platform_app, caplog
):
    """Sage's refresh token goes to its revoke endpoint, the client as form
    fields (its token endpoint's way), and the confirmation says it is gone."""
    revoke = _ScriptedRevoke(200).install(monkeypatch)
    caplog.set_level("DEBUG")

    result, session = await _delete_with_grant(monkeypatch, "sage_accounting")

    (call,) = revoke.calls
    assert call["url"] == SAGE_ACCOUNTING_OAUTH.revoke_url
    assert call["data"] == {
        "token": "REFRESH-SECRET-9",
        "client_id": "sage-platform-client",
        "client_secret": "sage-platform-secret",
    }
    assert call["auth"] is None, "Sage takes the client as form fields, never HTTP Basic"
    assert (result.erp_grant_provider, result.erp_grant_revocation) == (
        "sage_accounting",
        erp_oauth.REVOKED,
    )
    assert session.deletes == len(CONTROL_DELETIONS)
    assert "sage_accounting — revoked at the provider" in format_confirmation(result)
    for secret in (*_GRANT_TOKENS.values(), "sage-platform-secret"):
        assert secret not in caplog.text, "a token or the client secret reached the log"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(400, None), (503, None), (None, httpx.ConnectError("down"))],
    ids=["refused", "outage", "unreachable"],
)
async def test_a_revoke_that_fails_never_stops_the_deletion(
    monkeypatch, sage_platform_app, caplog, status, error
):
    revoke = _ScriptedRevoke(status or 200, error).install(monkeypatch)
    caplog.set_level("DEBUG")

    result, session = await _delete_with_grant(monkeypatch, "sage_accounting")

    assert len(revoke.calls) == 1
    assert result.erp_grant_revocation == erp_oauth.REVOKE_FAILED
    assert session.deletes == len(CONTROL_DELETIONS), "the control plane must still go"
    confirmation = format_confirmation(result)
    assert "sage_accounting — NOT revoked" in confirmation
    assert "connected apps" in confirmation
    assert "REFRESH-SECRET-9" not in caplog.text


@pytest.mark.asyncio
async def test_a_provider_with_no_revoke_endpoint_is_reported_not_called(monkeypatch):
    """Blackbaud SKY has no revocation endpoint: nothing is sent, and the
    confirmation tells the operator the customer must remove the app."""
    revoke = _ScriptedRevoke(200).install(monkeypatch)

    result, session = await _delete_with_grant(monkeypatch, "blackbaud_fe_nxt")

    assert revoke.calls == []
    assert result.erp_grant_revocation == erp_oauth.REVOKE_UNSUPPORTED
    assert session.deletes == len(CONTROL_DELETIONS)
    assert "no revocation endpoint" in format_confirmation(result)


@pytest.mark.asyncio
async def test_an_unopenable_store_is_reported_and_the_deletion_proceeds(monkeypatch):
    revoke = _ScriptedRevoke(200).install(monkeypatch)

    result, session = await _delete_with_grant(monkeypatch, "xero", unreadable=True)

    assert revoke.calls == []
    assert (result.erp_grant_provider, result.erp_grant_revocation) == (
        "xero",
        tenant_deletion.GRANT_UNREADABLE,
    )
    assert session.deletes == len(CONTROL_DELETIONS)
    assert "sealed tokens could not be opened" in format_confirmation(result)


@pytest.mark.asyncio
async def test_a_tenant_without_an_erp_grant_calls_nobody(monkeypatch):
    revoke = _ScriptedRevoke(200).install(monkeypatch)

    result, _session = await _delete_with_grant(monkeypatch, None)

    assert revoke.calls == []
    assert result.erp_grant_provider is None
    assert "ERP OAuth grant" not in format_confirmation(result)


def _closing_plan() -> _FakePlan:
    return _FakePlan(
        organization_id=uuid.uuid4(),
        slug="closing",
        name="Closing Co",
        db_name="feoh_closing",
        database_exists=False,
        control_rows={},
    )


@pytest.mark.asyncio
async def test_the_grant_is_read_on_the_closures_own_session(monkeypatch, sage_platform_app):
    """A self-service closure holds the org row FOR UPDATE on its session; the
    grant must be read there, not on a second session (decisions §306)."""
    _ScriptedRevoke(200).install(monkeypatch)
    plan = _closing_plan()
    held = _GrantSession(_grant_settings("sage_accounting"))
    seen: list[object] = []

    async def _resolved(_org_id, got_settings, db):
        seen.append(db)
        erp = dict(got_settings["erp"])
        oauth = dict(erp.pop("oauth"))
        oauth.update(_GRANT_TOKENS)
        return erp, oauth

    other = _GrantSession(None)  # what a second session would read: no grant
    monkeypatch.setattr(erp_oauth, "resolved_erp", _resolved)
    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)),
        patch.object(tenant_deletion, "control_session_factory", lambda: other),
    ):
        result = await delete_tenant("closing", control_session=held)
    assert seen == [held]
    # Inside a savepoint, so a failed read cannot poison the closure's
    # transaction before its control-plane delete.
    assert held.savepoints == 1
    assert held.deletes == len(CONTROL_DELETIONS) and other.deletes == 0
    assert result.erp_grant_provider == "sage_accounting"
    await held.commit()
    assert result.erp_grant_revocation == erp_oauth.REVOKED


@pytest.mark.asyncio
async def test_a_closure_calls_the_provider_only_after_its_commit(
    monkeypatch, sage_platform_app, caplog
):
    """The closure's transaction holds the org, user and subscription rows
    FOR UPDATE until its caller commits. The provider is never called inside
    it (root CLAUDE.md, `post_commit`): the tokens are read there, and the
    revoke is queued for the commit. The outcome still reaches the result and
    the log, provider and outcome only."""
    revoke = _ScriptedRevoke(200).install(monkeypatch)
    caplog.set_level("DEBUG")
    plan = _closing_plan()
    held = _GrantSession(_grant_settings("sage_accounting"))

    async def _resolved(_org_id, got_settings, _db):
        erp = dict(got_settings["erp"])
        oauth = dict(erp.pop("oauth"))
        oauth.update(_GRANT_TOKENS)
        return erp, oauth

    monkeypatch.setattr(erp_oauth, "resolved_erp", _resolved)
    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)),
        patch.object(tenant_deletion, "control_session_factory", lambda: _GrantSession(None)),
    ):
        result = await delete_tenant("closing", control_session=held)

    # Everything the closure's transaction does is done, and it is still open.
    assert held.deletes == len(CONTROL_DELETIONS)
    assert revoke.calls == [], "the provider was called while the closure held its locks"
    assert (result.erp_grant_provider, result.erp_grant_revocation) == (
        "sage_accounting",
        tenant_deletion.GRANT_REVOKE_AFTER_COMMIT,
    )
    assert "NOT yet confirmed" in format_confirmation(result)

    await held.commit()
    (call,) = revoke.calls
    assert call["data"]["token"] == "REFRESH-SECRET-9"
    assert result.erp_grant_revocation == erp_oauth.REVOKED
    assert "ERP OAuth grant (sage_accounting) revoked" in caplog.text
    for secret in (*_GRANT_TOKENS.values(), "sage-platform-secret"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_a_closure_whose_provider_refuses_after_commit_reports_failed(
    monkeypatch, sage_platform_app, caplog
):
    """After the commit nothing can stop the deletion, and nothing should: a
    refusal is logged as ``failed`` and lands on the result."""
    revoke = _ScriptedRevoke(503).install(monkeypatch)
    caplog.set_level("DEBUG")
    held = _GrantSession(_grant_settings("sage_accounting"))

    async def _resolved(_org_id, got_settings, _db):
        erp = dict(got_settings["erp"])
        oauth = dict(erp.pop("oauth"))
        oauth.update(_GRANT_TOKENS)
        return erp, oauth

    monkeypatch.setattr(erp_oauth, "resolved_erp", _resolved)
    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=_closing_plan())),
        patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)),
        patch.object(tenant_deletion, "control_session_factory", lambda: _GrantSession(None)),
    ):
        result = await delete_tenant("closing", control_session=held)
    assert revoke.calls == []
    await held.commit()
    assert len(revoke.calls) == 1
    assert result.erp_grant_revocation == erp_oauth.REVOKE_FAILED
    assert "ERP OAuth grant (sage_accounting) failed" in caplog.text


@pytest.mark.asyncio
async def test_a_closure_that_rolls_back_revokes_nothing(monkeypatch, sage_platform_app):
    """A rolled-back closure leaves the organisation and its sealed row; the
    resumed closure reads and revokes again. Revoking now would cut a grant
    whose workspace still says connected."""
    revoke = _ScriptedRevoke(200).install(monkeypatch)
    held = _GrantSession(_grant_settings("sage_accounting"))

    async def _resolved(_org_id, got_settings, _db):
        erp = dict(got_settings["erp"])
        oauth = dict(erp.pop("oauth"))
        oauth.update(_GRANT_TOKENS)
        return erp, oauth

    monkeypatch.setattr(erp_oauth, "resolved_erp", _resolved)
    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=_closing_plan())),
        patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)),
        patch.object(tenant_deletion, "control_session_factory", lambda: _GrantSession(None)),
    ):
        await delete_tenant("closing", control_session=held)
    held.rollback()
    await held.commit()  # a later commit on the session runs nothing queued before
    assert revoke.calls == []


@pytest.mark.asyncio
async def test_a_closure_with_a_provider_without_an_endpoint_reports_it_at_once(monkeypatch):
    """Nothing to call, so nothing to defer: Blackbaud reads ``unsupported``
    on the result straight away, and the commit sends nothing."""
    revoke = _ScriptedRevoke(200).install(monkeypatch)
    held = _GrantSession(_grant_settings("blackbaud_fe_nxt"))

    async def _resolved(_org_id, got_settings, _db):
        erp = dict(got_settings["erp"])
        oauth = dict(erp.pop("oauth"))
        oauth.update(_GRANT_TOKENS)
        return erp, oauth

    monkeypatch.setattr(erp_oauth, "resolved_erp", _resolved)
    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=_closing_plan())),
        patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)),
        patch.object(tenant_deletion, "control_session_factory", lambda: _GrantSession(None)),
    ):
        result = await delete_tenant("closing", control_session=held)
    await held.commit()
    assert revoke.calls == []
    assert result.erp_grant_revocation == erp_oauth.REVOKE_UNSUPPORTED


@pytest.mark.asyncio
async def test_a_failed_grant_read_on_the_closures_session_still_deletes(monkeypatch):
    """A lock timeout (or any SQL error) reading the grant is reported as
    ``failed``; the read ran in a savepoint, so the closure's own transaction
    still carries the control-plane delete."""
    revoke = _ScriptedRevoke(200).install(monkeypatch)
    plan = _FakePlan(
        organization_id=uuid.uuid4(),
        slug="closing",
        name="Closing Co",
        db_name="feoh_closing",
        database_exists=False,
        control_rows={},
    )
    held = _GrantSession(_grant_settings("xero"))

    async def _timeout(*_a, **_k):
        raise RuntimeError("canceling statement due to lock timeout")

    monkeypatch.setattr(erp_oauth, "resolved_erp", _timeout)
    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)),
        patch.object(tenant_deletion, "control_session_factory", lambda: _GrantSession(None)),
    ):
        result = await delete_tenant("closing", control_session=held)
    assert revoke.calls == []
    assert (result.erp_grant_provider, result.erp_grant_revocation) == (
        "xero",
        erp_oauth.REVOKE_FAILED,
    )
    assert held.savepoints == 1
    assert held.deletes == len(CONTROL_DELETIONS)


def test_every_grant_outcome_has_its_confirmation_text():
    plan = _FakePlan(
        organization_id=uuid.uuid4(),
        slug="conf",
        name="Confirm Co",
        db_name="feoh_conf",
        database_exists=True,
        control_rows={},
    )
    outcomes = (
        erp_oauth.REVOKED,
        erp_oauth.REVOKE_FAILED,
        erp_oauth.REVOKE_UNSUPPORTED,
        tenant_deletion.GRANT_UNREADABLE,
        tenant_deletion.GRANT_REVOKE_AFTER_COMMIT,
    )
    for outcome in outcomes:
        text_out = format_confirmation(
            tenant_deletion.TenantDeletionResult(
                plan=plan,
                objects_deleted=0,
                database_dropped=True,
                erp_grant_provider="xero",
                erp_grant_revocation=outcome,
            )
        )
        line = next(ln for ln in text_out.splitlines() if "ERP OAuth grant" in ln)
        if outcome == erp_oauth.REVOKED:
            assert "revoked at the provider" in line
        elif outcome == tenant_deletion.GRANT_REVOKE_AFTER_COMMIT:
            assert "NOT yet confirmed" in line and "connected apps" in line
        else:
            assert "NOT revoked" in line and "connected apps" in line, outcome


# ---------------------------------------------------------------------------
# Against a live control plane
# ---------------------------------------------------------------------------


async def _control_engine_and_maker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def _deletion_on_test_loop():
    """Bind provisioning AND deletion to an engine on THIS test's loop.

    Same reason as `test_tenant_provisioning`'s equivalent: the module-global
    factory binds to whichever loop first drove it, so a later test on a fresh
    per-test loop otherwise fails with 'attached to a different loop'.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.services.tenant_provisioning as tp

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with (
        patch.object(tp, "control_session_factory", factory),
        patch.object(tenant_deletion, "control_session_factory", factory),
    ):
        yield
    await engine.dispose()


@pytest.fixture
def throwaway_slug():
    return "del" + uuid.uuid4().hex[:8]


async def _provision(slug: str):
    return await provision_tenant(
        company_name=f"Deletion Test {slug}",
        slug=slug,
        admin_email=f"admin@{slug}.test",
        admin_name="Deletion Admin",
        admin_password="Sup3rSecret!pw",
    )


async def test_delete_tenant_removes_every_trace_from_the_control_plane(
    realdb, throwaway_slug, _deletion_on_test_loop
):
    slug = throwaway_slug
    await _provision(slug)

    engine, mk = await _control_engine_and_maker()
    try:
        async with mk() as s:
            org_id = (
                await s.execute(text("SELECT id FROM organizations WHERE slug = :s"), {"s": slug})
            ).scalar_one()
            # A row in a table nothing else in the suite exercises for deletion:
            # no FK guards it, so a forgotten sweep would be invisible.
            await s.execute(
                text(
                    "INSERT INTO assistant_usage (id, organization_id, period, input_tokens, "
                    "output_tokens, request_count, created_at, updated_at) VALUES "
                    "(:id, :org, '2026-09', 10, 20, 1, now(), now())"
                ),
                {"id": uuid.uuid4(), "org": org_id},
            )
            await s.commit()

        # Storage is patched: MinIO has nothing under this org, and what matters
        # here is that the sweep is asked for the right prefix.
        swept: list[str] = []

        async def _sweep(prefix):
            swept.append(prefix)
            return 0

        with patch.object(tenant_deletion.storage, "delete_prefix", _sweep):
            result = await delete_tenant(slug)

        # Twice: the second pass catches an object PUT during the first listing.
        assert swept == [f"{org_id}/", f"{org_id}/"]
        assert result.database_dropped is True

        async with mk() as s:
            for table, _sql in CONTROL_DELETIONS:
                column = "slug" if table == "email_verifications" else "organization_id"
                if table == "organizations":
                    remaining = (
                        await s.execute(
                            text("SELECT COUNT(*) FROM organizations WHERE id = :org"),
                            {"org": org_id},
                        )
                    ).scalar_one()
                elif table in {"webauthn_credentials", "user_roles"}:
                    continue  # keyed through users, which are gone (asserted below)
                elif column == "slug":
                    remaining = (
                        await s.execute(
                            text("SELECT COUNT(*) FROM email_verifications WHERE slug = :s"),
                            {"s": slug},
                        )
                    ).scalar_one()
                else:
                    remaining = (
                        await s.execute(
                            text(f"SELECT COUNT(*) FROM {table} WHERE organization_id = :org"),
                            {"org": org_id},
                        )
                    ).scalar_one()
                assert remaining == 0, f"{table} still holds rows for the deleted tenant"

            users_left = (
                await s.execute(
                    text("SELECT COUNT(*) FROM users WHERE organization_id = :org"),
                    {"org": org_id},
                )
            ).scalar_one()
            assert users_left == 0

            # The four system roles are shared by every tenant on the platform.
            system_roles = (
                await s.execute(text("SELECT COUNT(*) FROM roles WHERE organization_id IS NULL"))
            ).scalar_one()
            assert system_roles >= 4, "a tenant deletion removed the shared system roles"

            db_left = (
                await s.execute(
                    text("SELECT COUNT(*) FROM pg_database WHERE datname = :db"),
                    {"db": f"{settings.tenant_db_prefix}{slug}"},
                )
            ).scalar_one()
            assert db_left == 0
    finally:
        await engine.dispose()


async def test_delete_tenant_leaves_a_neighbouring_org_untouched(
    realdb, throwaway_slug, _deletion_on_test_loop
):
    doomed = throwaway_slug
    neighbour = "keep" + uuid.uuid4().hex[:8]
    await _provision(doomed)
    await _provision(neighbour)

    engine, mk = await _control_engine_and_maker()
    try:
        with patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)):
            await delete_tenant(doomed)

        async with mk() as s:
            still_there = (
                await s.execute(
                    text("SELECT COUNT(*) FROM organizations WHERE slug = :s"), {"s": neighbour}
                )
            ).scalar_one()
            assert still_there == 1
            neighbour_users = (
                await s.execute(
                    text(
                        "SELECT COUNT(*) FROM users u JOIN organizations o "
                        "ON o.id = u.organization_id WHERE o.slug = :s"
                    ),
                    {"s": neighbour},
                )
            ).scalar_one()
            assert neighbour_users == 1

        # Clean up the survivor.
        with patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)):
            await delete_tenant(neighbour)
    finally:
        await engine.dispose()


async def test_deleting_an_unknown_slug_refuses_rather_than_reporting_success(
    realdb, _deletion_on_test_loop
):
    with pytest.raises(tenant_deletion.TenantNotFound, match="no organisation with slug"):
        await plan_tenant_deletion("nosuchtenant" + uuid.uuid4().hex[:6])


async def test_a_partner_org_with_children_is_refused(
    realdb, throwaway_slug, _deletion_on_test_loop
):
    """`parent_org_id` is ON DELETE SET NULL, so Postgres would accept this and
    silently promote every child tenant to standalone — a reseller's customers
    detached from the partner that administers them, with nothing saying so."""
    parent = throwaway_slug
    child = "kid" + uuid.uuid4().hex[:8]
    await _provision(parent)
    await _provision(child)

    engine, mk = await _control_engine_and_maker()
    try:
        async with mk() as s:
            await s.execute(
                text(
                    "UPDATE organizations SET parent_org_id = "
                    "(SELECT id FROM organizations WHERE slug = :p) WHERE slug = :c"
                ),
                {"p": parent, "c": child},
            )
            await s.commit()

        with pytest.raises(TenantDeletionError, match="partner organisation"):
            await delete_tenant(parent)

        # And the refusal really did leave everything in place.
        async with mk() as s:
            assert (
                await s.execute(
                    text("SELECT COUNT(*) FROM organizations WHERE slug IN (:p, :c)"),
                    {"p": parent, "c": child},
                )
            ).scalar_one() == 2

            # Detach and clean up.
            await s.execute(
                text("UPDATE organizations SET parent_org_id = NULL WHERE slug = :c"), {"c": child}
            )
            await s.commit()

        with patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)):
            await delete_tenant(child)
            await delete_tenant(parent)
    finally:
        await engine.dispose()


# ── The CLI's exit codes — deploy/remove-tenant.sh branches on them ──────────


def _load_delete_tenant_cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "delete_tenant.py"
    spec = importlib.util.spec_from_file_location("_delete_tenant_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


@pytest.mark.parametrize(
    ("error", "code"),
    [
        # The ONE code remove-tenant.sh reads as "already gone, carry on".
        (tenant_deletion.TenantNotFound("no organisation with slug 'x'"), 4),
        # Every other refusal stops it before it deletes the backups.
        (TenantDeletionError("'x' is a partner organisation with 2 child tenant(s)"), 2),
    ],
)
async def test_the_cli_exit_code_tells_not_found_from_a_refusal(monkeypatch, capsys, error, code):
    cli = _load_delete_tenant_cli()
    monkeypatch.setattr(cli, "plan_tenant_deletion", AsyncMock(side_effect=error))
    monkeypatch.setattr(cli, "delete_tenant", AsyncMock(side_effect=AssertionError("deleted")))
    monkeypatch.setattr("sys.argv", ["delete_tenant.py", "--slug", "x", "--dry-run"])

    assert await cli.main() == code
    assert str(error) in capsys.readouterr().err


async def test_the_cli_refuses_to_delete_without_confirmation(monkeypatch):
    cli = _load_delete_tenant_cli()
    monkeypatch.setattr(
        cli,
        "plan_tenant_deletion",
        AsyncMock(return_value=_FakePlan(uuid.uuid4(), "x", "X", "feoh_x", True, {})),
    )
    monkeypatch.setattr(cli, "_print_plan", lambda _plan: None)
    monkeypatch.setattr(cli, "delete_tenant", AsyncMock(side_effect=AssertionError("deleted")))
    monkeypatch.setattr("sys.argv", ["delete_tenant.py", "--slug", "x", "--confirm", "y"])

    assert await cli.main() == 3


async def test_the_cli_fails_when_the_deletion_itself_refuses(monkeypatch):
    cli = _load_delete_tenant_cli()
    monkeypatch.setattr(
        cli,
        "plan_tenant_deletion",
        AsyncMock(return_value=_FakePlan(uuid.uuid4(), "x", "X", "feoh_x", True, {})),
    )
    monkeypatch.setattr(cli, "_print_plan", lambda _plan: None)
    monkeypatch.setattr(
        cli, "delete_tenant", AsyncMock(side_effect=TenantDeletionError("refused mid-way"))
    )
    monkeypatch.setattr("sys.argv", ["delete_tenant.py", "--slug", "x", "--yes"])

    assert await cli.main() == 2
