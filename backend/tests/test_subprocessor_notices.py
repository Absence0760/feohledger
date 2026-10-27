"""Sub-processor change notices reach each organisation once, on the record.

Issue #427 / decisions §321: a ``notice=True`` change-log entry is emailed to
every active administrator of every organisation that existed when it was
published; the (entry, organisation) pair is claimed in
``subprocessor_notices`` before anything is sent, so a re-run or a second
replica cannot notify twice; a tenant audit row is the trail; and nobody being
reachable rolls the claim back so the next tick retries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.models.organization import Organization
from app.models.subprocessor_notice import SubprocessorNotice
from app.models.user import Role, User, UserRole
from app.models.workflow import AuditLog
from app.services import subprocessor_notices as sn
from app.services.subprocessor_changelog import (
    NOTICE_PERIOD_DAYS,
    OBJECTION_CONTACT,
    ChangelogEntry,
)
from app.services.sweep_health import SWEEP_ENABLED_FLAGS, SWEEP_SUBPROCESSOR_NOTICES

NOW = datetime.now(UTC)
TODAY = NOW.date()


def _entry(*, days: int = NOTICE_PERIOD_DAYS, urgent: str | None = None, published=None):
    published = published or TODAY
    return ChangelogEntry(
        id=f"{published.isoformat()}-t{uuid.uuid4().hex[:10]}",
        published_on=published,
        change="ExampleCo is added to § 2 for invoice extraction.",
        notice=True,
        effective_on=published + timedelta(days=days),
        urgent_reason=urgent,
    )


class _Outbox:
    def __init__(self, fail_for: set[str] | None = None):
        self.sent = []
        self.fail_for = fail_for or set()

    async def send(self, message):
        if message.to in self.fail_for:
            raise ConnectionError("provider refused")
        self.sent.append(message)


@pytest.fixture
def outbox(monkeypatch):
    box = _Outbox()
    monkeypatch.setattr(sn, "get_email_adapter", lambda: box)
    return box


def _nullpool_engine(db_name: str):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import _make_tenant_url

    return create_async_engine(_make_tenant_url(db_name), poolclass=NullPool)


@pytest.fixture
def _tenant_engine_on_loop(monkeypatch):
    """The audit row's tenant engine on THIS test's loop (the dunning fixture)."""
    monkeypatch.setattr("app.database.get_tenant_engine", _nullpool_engine)


async def _ledger(realdb, entry_id: str) -> list[SubprocessorNotice]:
    async with realdb.control_sessionmaker()() as s:
        return list(
            (
                await s.execute(
                    select(SubprocessorNotice).where(SubprocessorNotice.entry_id == entry_id)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_the_notice_says_what_changes_when_and_how_to_object():
    entry = _entry()
    subject, body = sn.render_notice(
        entry, org_name="Acme Ltd", recipient_name="Dana", sent_on=TODAY
    )
    assert "Acme Ltd" in subject
    assert entry.change in body
    assert OBJECTION_CONTACT in body
    assert f"within {NOTICE_PERIOD_DAYS} days" in body
    assert "/legal/sub-processors#change-log" in body
    assert "/legal/dpa#sub-processor-changes" in body
    assert "cannot be turned off" in body
    assert "less than" not in body


def test_an_urgent_notice_states_its_reason():
    entry = _entry(days=5, urgent="Our current provider is shutting down.")
    _, body = sn.render_notice(entry, org_name="Acme", recipient_name="Dana", sent_on=TODAY)
    assert "less than 30 days' notice" in body
    assert "shutting down" in body


def test_a_notice_sent_after_the_change_does_not_claim_a_future_date():
    entry = _entry(days=NOTICE_PERIOD_DAYS)
    _, on_time = sn.render_notice(entry, org_name="A", recipient_name="D", sent_on=TODAY)
    assert "Takes effect:" in on_time
    _, after = sn.render_notice(entry, org_name="A", recipient_name="D", sent_on=entry.effective_on)
    assert "Took effect:" in after
    assert "Takes effect:" not in after


def test_lateness_is_measured_against_the_effective_date():
    entry = _entry(days=NOTICE_PERIOD_DAYS)
    assert not sn.is_late(entry, entry.published_on)
    assert sn.is_late(entry, entry.published_on + timedelta(days=1))


def test_the_sweep_is_registered_and_off_by_default():
    from app.config import Settings

    assert SWEEP_ENABLED_FLAGS[SWEEP_SUBPROCESSOR_NOTICES] == "subprocessor_notices_enabled"
    assert Settings.model_fields["subprocessor_notices_enabled"].default is False


# ---------------------------------------------------------------------------
# Real database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_active_admins_are_told_and_the_notice_is_recorded(
    realdb, outbox, _tenant_engine_on_loop
):
    entry = _entry()
    info = realdb.info("a")
    async with realdb.control_sessionmaker()() as s:
        sent = await sn.notify_organization(s, entry, info.org_id, now=NOW)
    assert sent == 1
    # The harness seeds one user per role; only the admin is a recipient.
    assert [m.to for m in outbox.sent] == [realdb.email("a", "admin")]
    assert outbox.sent[0].brand is None  # the platform's notice, never white-labelled

    [row] = await _ledger(realdb, entry.id)
    assert row.organization_id == info.org_id
    assert (row.recipients, row.failed_recipients, row.late) == (1, 0, False)
    assert row.effective_on == entry.effective_on
    assert row.sent_at is not None
    assert row.recipient_user_ids == [str(info.users["admin"])]
    assert row.audited_at is None  # the audit is its own step

    async with realdb.control_sessionmaker()() as s:
        assert await sn._write_audit(s, row.id, now=NOW) is True
        assert await sn._write_audit(s, row.id, now=NOW) is False  # once
    [row] = await _ledger(realdb, entry.id)
    assert row.audited_at is not None

    async with realdb.sessionmaker("a")() as t:
        [audit] = (
            (
                await t.execute(
                    select(AuditLog).where(
                        AuditLog.action == sn.AUDIT_ACTION,
                        AuditLog.details["entry_id"].astext == entry.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert audit.entity_id == info.org_id
    assert audit.actor_id is None
    assert audit.details["recipient_user_ids"] == [str(info.users["admin"])]
    assert audit.details["notice_days"] == NOTICE_PERIOD_DAYS
    assert audit.details["late"] is False
    # Ids only on the trail — never an address.
    assert realdb.email("a", "admin") not in str(audit.details)


@pytest.mark.asyncio
async def test_an_organisation_is_told_once_per_entry(realdb, outbox, _tenant_engine_on_loop):
    entry = _entry()
    org_id = realdb.info("a").org_id
    async with realdb.control_sessionmaker()() as s:
        assert await sn.notify_organization(s, entry, org_id, now=NOW) == 1
        assert await sn.notify_organization(s, entry, org_id, now=NOW) is None
    assert len(outbox.sent) == 1
    assert len(await _ledger(realdb, entry.id)) == 1


@pytest.mark.asyncio
async def test_nobody_reachable_leaves_no_record_so_the_next_tick_retries(
    realdb, monkeypatch, _tenant_engine_on_loop
):
    entry = _entry()
    box = _Outbox(fail_for={realdb.email("b", "admin")})
    monkeypatch.setattr(sn, "get_email_adapter", lambda: box)
    org_id = realdb.info("b").org_id
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(sn.NoNoticeDelivered):
            await sn.notify_organization(s, entry, org_id, now=NOW)
    assert await _ledger(realdb, entry.id) == []

    box.fail_for.clear()
    async with realdb.control_sessionmaker()() as s:
        assert await sn.notify_organization(s, entry, org_id, now=NOW) == 1
    assert len(await _ledger(realdb, entry.id)) == 1


@pytest.mark.asyncio
async def test_one_unreachable_admin_does_not_stop_the_others(
    realdb, monkeypatch, _tenant_engine_on_loop
):
    info = realdb.info("b")
    extra_id = uuid.uuid4()
    extra_email = f"second-admin-{extra_id.hex[:8]}@{info.slug}.test"
    async with realdb.control_sessionmaker()() as s:
        admin_role = (
            await s.execute(
                select(Role.id).where(Role.name == "admin", Role.organization_id.is_(None))
            )
        ).scalar_one()
        s.add(
            User(
                id=extra_id,
                email=extra_email,
                full_name="Second Admin",
                hashed_password=None,
                is_active=True,
                organization_id=info.org_id,
                must_change_password=False,
            )
        )
        await s.flush()
        s.add(UserRole(user_id=extra_id, role_id=admin_role))
        await s.commit()
    try:
        box = _Outbox(fail_for={extra_email})
        monkeypatch.setattr(sn, "get_email_adapter", lambda: box)
        entry = _entry()
        async with realdb.control_sessionmaker()() as s:
            assert await sn.notify_organization(s, entry, info.org_id, now=NOW) == 1
        [row] = await _ledger(realdb, entry.id)
        assert (row.recipients, row.failed_recipients) == (1, 1)
        # Never re-sent, so the miss must be visible: the operator's list...
        async with realdb.control_sessionmaker()() as s:
            partial = [p for p in await sn.partial_deliveries(s) if p.entry_id == entry.id]
        assert [(p.organization_id, p.recipients, p.failed_recipients) for p in partial] == [
            (info.org_id, 1, 1)
        ]
        # ...and the tick's counters on /api/health/sweeps.
        ticked = _entry()
        async with realdb.control_sessionmaker()() as s:
            result = await sn.run_notices_once(s, now=NOW, entries=(ticked,))
        assert result.failed_recipients == 1
        assert result.failures == 0
    finally:
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(UserRole).where(UserRole.user_id == extra_id))
            await s.execute(delete(User).where(User.id == extra_id))
            await s.commit()


@pytest.mark.asyncio
async def test_a_failed_audit_is_retried_without_sending_the_notice_again(
    realdb, outbox, monkeypatch, _tenant_engine_on_loop
):
    entry = _entry()
    ours = {realdb.info("a").org_id, realdb.info("b").org_id}

    async def _broken(*args, **kwargs):
        raise ConnectionError("tenant database unreachable")

    monkeypatch.setattr(sn, "log_action", _broken)
    async with realdb.control_sessionmaker()() as s:
        first = await sn.run_notices_once(s, now=NOW, entries=(entry,))
    assert first.failures >= 2  # one audit failure per organisation
    rows = {r.organization_id: r for r in await _ledger(realdb, entry.id)}
    assert ours <= set(rows)
    assert all(rows[o].sent_at is not None and rows[o].audited_at is None for o in ours)
    sent_before = len(outbox.sent)

    monkeypatch.undo()
    monkeypatch.setattr(sn, "get_email_adapter", lambda: outbox)
    monkeypatch.setattr(
        "app.database.get_tenant_engine",
        _nullpool_engine,
    )
    async with realdb.control_sessionmaker()() as s:
        second = await sn.run_notices_once(s, now=NOW, entries=(entry,))
    assert len(outbox.sent) == sent_before  # never re-sent
    assert second.audits_written >= 2
    rows = {r.organization_id: r for r in await _ledger(realdb, entry.id)}
    assert all(rows[o].audited_at is not None for o in ours)


@pytest.mark.asyncio
async def test_a_claim_abandoned_mid_send_is_retaken_but_a_live_one_is_not(realdb, outbox):
    entry = _entry()
    org_id = realdb.info("a").org_id
    async with realdb.control_sessionmaker()() as s:
        s.add(
            SubprocessorNotice(
                entry_id=entry.id,
                organization_id=org_id,
                claimed_at=NOW - timedelta(minutes=5),
                effective_on=entry.effective_on,
                recipients=0,
                failed_recipients=0,
                late=False,
            )
        )
        await s.commit()
    async with realdb.control_sessionmaker()() as s:
        # Another run is mid-send: leave it alone.
        assert await sn.notify_organization(s, entry, org_id, now=NOW) is None
        [owed] = await sn.owed_notices(s, now=NOW, entries=(entry,))
        assert org_id not in owed.organization_ids
        # An hour later that run has evidently died: the notice is still owed.
        later = NOW + sn.CLAIM_TIMEOUT
        [owed] = await sn.owed_notices(s, now=later, entries=(entry,))
        assert org_id in owed.organization_ids
        assert await sn.notify_organization(s, entry, org_id, now=later) == 1
    [row] = await _ledger(realdb, entry.id)
    assert row.sent_at is not None


async def _notice_audit_rows(realdb, slot: str, notice_id: uuid.UUID) -> list[AuditLog]:
    async with realdb.sessionmaker(slot)() as t:
        return list(
            (
                await t.execute(
                    select(AuditLog).where(
                        AuditLog.action == sn.AUDIT_ACTION, AuditLog.correlation_id == notice_id
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_a_stamp_lost_after_the_audit_row_landed_never_appends_a_second(
    realdb, outbox, _tenant_engine_on_loop
):
    # The tenant row and the control-plane `audited_at` stamp are two commits in
    # two databases. A stamp that fails after the tenant commit sends the next
    # tick back to the same notice; the append-only trail must still hold one row.
    entry = _entry()
    async with realdb.control_sessionmaker()() as s:
        await sn.notify_organization(s, entry, realdb.info("a").org_id, now=NOW)
    [row] = await _ledger(realdb, entry.id)
    async with realdb.control_sessionmaker()() as s:
        assert await sn._write_audit(s, row.id, now=NOW) is True
        # The stamp "never landed": exactly the state a failed control commit leaves.
        await s.execute(
            SubprocessorNotice.__table__.update()
            .where(SubprocessorNotice.id == row.id)
            .values(audited_at=None)
        )
        await s.commit()
        assert await sn._write_audit(s, row.id, now=NOW) is True
    assert len(await _notice_audit_rows(realdb, "a", row.id)) == 1
    [row] = await _ledger(realdb, entry.id)
    assert row.audited_at is not None


@pytest.mark.asyncio
async def test_the_audit_row_is_written_before_the_stamp_in_lambda_audit_mode(
    realdb, outbox, monkeypatch, _tenant_engine_on_loop
):
    # In lambda mode the ordinary dispatch only enqueues to SQS, so "absent from
    # the tenant" could mean "in flight" and a retry would duplicate it. The
    # notice's row is written synchronously in every mode, never via SQS.
    from app.config import settings
    from app.services import audit_dispatch

    def _no_sqs(**kwargs):
        raise AssertionError("the notice's audit row must not go through SQS")

    monkeypatch.setattr(settings, "audit_mode", "lambda")
    monkeypatch.setattr(audit_dispatch, "_send_to_sqs", _no_sqs)
    entry = _entry()
    async with realdb.control_sessionmaker()() as s:
        await sn.notify_organization(s, entry, realdb.info("a").org_id, now=NOW)
    [row] = await _ledger(realdb, entry.id)
    async with realdb.control_sessionmaker()() as s:
        assert await sn._write_audit(s, row.id, now=NOW) is True
    assert len(await _notice_audit_rows(realdb, "a", row.id)) == 1


@pytest.mark.asyncio
async def test_an_organisation_with_no_active_admin_is_counted_apart_not_failed(
    realdb, outbox, _tenant_engine_on_loop
):
    # Nobody to tell is not the sweep failing: counting it as a failure turned
    # /api/health/sweeps red on every tick for as long as such an org existed.
    entry = _entry(published=TODAY - timedelta(days=1))
    orphan = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            Organization(
                id=orphan,
                name="No Admin Scratch",
                slug=f"spn-{orphan.hex[:12]}",
                db_name=f"feoh_spn_{orphan.hex[:12]}",
                created_at=NOW - timedelta(days=10),
            )
        )
        await s.commit()
    try:
        async with realdb.control_sessionmaker()() as s:
            with pytest.raises(sn.NoActiveAdministrator):
                await sn.notify_organization(s, entry, orphan, now=NOW)
            result = await sn.run_notices_once(s, now=NOW, entries=(entry,))
        assert result.organizations_without_admin == 1
        assert result.failures == 0
        assert orphan not in {r.organization_id for r in await _ledger(realdb, entry.id)}
        # Still owed: it is told once it has an administrator.
        async with realdb.control_sessionmaker()() as s:
            [owed] = await sn.owed_notices(s, now=NOW, entries=(entry,))
        assert orphan in owed.organization_ids

        from app.services.sweep_health import extract_counts, failure_count

        counts = extract_counts(result)
        assert counts["organizations_without_admin"] == 1
        assert failure_count(counts) == 0
    finally:
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(Organization).where(Organization.id == orphan))
            await s.commit()


def test_a_line_break_in_the_organisation_name_cannot_split_the_subject():
    from app.services.email_adapters.base import EmailMessage

    subject, _ = sn.render_notice(
        _entry(), org_name="Acme\r\nBcc: x@evil.test", recipient_name="D", sent_on=TODAY
    )
    message = EmailMessage(to="d@acme.test", subject=subject, body_text="x")
    assert "\r" not in message.subject and "\n" not in message.subject
    assert "Acme Bcc: x@evil.test" in message.subject


@pytest.mark.asyncio
async def test_a_short_notice_is_recorded_as_late(realdb, outbox, _tenant_engine_on_loop):
    entry = _entry(days=7, urgent="Security: the current provider was breached.")
    async with realdb.control_sessionmaker()() as s:
        await sn.notify_organization(s, entry, realdb.info("a").org_id, now=NOW)
    [row] = await _ledger(realdb, entry.id)
    assert row.late is True
    assert "breached" in outbox.sent[0].body_text


@pytest.mark.asyncio
async def test_an_organisation_created_after_publication_is_not_owed(realdb):
    entry = _entry(published=TODAY - timedelta(days=2))
    before, after = uuid.uuid4(), uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        for org_id, created in ((before, NOW - timedelta(days=10)), (after, NOW)):
            s.add(
                Organization(
                    id=org_id,
                    name="Notice Scratch",
                    slug=f"spn-{org_id.hex[:12]}",
                    db_name=f"feoh_spn_{org_id.hex[:12]}",
                    created_at=created,
                )
            )
        await s.commit()
    try:
        async with realdb.control_sessionmaker()() as s:
            [owed] = await sn.owed_notices(s, now=NOW, entries=(entry,))
        assert before in owed.organization_ids
        assert after not in owed.organization_ids
    finally:
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(Organization).where(Organization.id.in_([before, after])))
            await s.commit()


@pytest.mark.asyncio
async def test_an_unpublished_or_plain_entry_is_never_sent(realdb, outbox):
    future = _entry(published=TODAY + timedelta(days=1))
    plain = ChangelogEntry(id=f"{TODAY.isoformat()}-plain", published_on=TODAY, change="x")
    async with realdb.control_sessionmaker()() as s:
        assert await sn.owed_notices(s, now=NOW, entries=(future, plain)) == []


@pytest.mark.asyncio
async def test_the_tick_notifies_every_owed_organisation_and_then_nothing(
    realdb, outbox, _tenant_engine_on_loop
):
    entry = _entry()
    ours = {realdb.info("a").org_id, realdb.info("b").org_id}
    async with realdb.control_sessionmaker()() as s:
        first = await sn.run_notices_once(s, now=NOW, entries=(entry,))
    assert first.entries_due == 1
    notified = {r.organization_id for r in await _ledger(realdb, entry.id)}
    assert ours <= notified
    assert first.organizations_notified == len(notified)
    assert first.late_notices == 0
    sent_to = {m.to for m in outbox.sent}
    assert {realdb.email("a", "admin"), realdb.email("b", "admin")} <= sent_to

    outbox.sent.clear()
    async with realdb.control_sessionmaker()() as s:
        second = await sn.run_notices_once(s, now=NOW, entries=(entry,))
    assert second.organizations_notified == 0
    assert not [m for m in outbox.sent if m.to in sent_to]


def test_the_cli_lists_and_sends_through_the_service(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "subprocessor_notices.py"
    spec = importlib.util.spec_from_file_location("_spn_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    entry = _entry()
    org = uuid.uuid4()

    async def _owed(db, *, now):
        return [sn.OwedNotice(entry=entry, organization_ids=[org])]

    async def _run(db):
        return sn.NoticeResult(
            organizations_notified=1,
            emails_sent=2,
            failed_recipients=1,
            organizations_without_admin=3,
            failures=1,
        )

    partial_org = uuid.uuid4()

    async def _partial(db):
        return [
            sn.PartialDelivery(
                entry_id=entry.id,
                organization_id=partial_org,
                sent_at=NOW,
                recipients=1,
                failed_recipients=2,
            )
        ]

    monkeypatch.setattr(cli, "control_session_factory", _Session)
    monkeypatch.setattr(cli, "owed_notices", _owed)
    monkeypatch.setattr(cli, "partial_deliveries", _partial)
    monkeypatch.setattr(cli, "run_notices_once", _run)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert entry.id in out
    # A notice some administrators missed is never re-sent, so the operator sees it.
    assert f"organisation {partial_org}" in out
    assert "2 administrator(s) not reached" in out
    assert cli.main(["send"]) == 1  # a failure is a non-zero exit
    out = capsys.readouterr().out
    assert "administrators not reached=1" in out
    assert "without an administrator=3" in out
