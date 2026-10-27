"""Send the DPA's sub-processor change notice to every customer organisation.

Issue #427 / `docs/decisions.md` §321. `/legal/dpa` § 8 promises at least 30
days' notice before a new sub-processor begins processing a customer's data.
The change log is data (`services/subprocessor_changelog.py`); this module is
the half that tells customers. Adding a ``notice=True`` entry to the change log
and deploying is the whole procedure: the published table regenerates from the
same entry, and this sweep emails it.

Who is told
-----------
Every **active holder of the system ``admin`` role** in each organisation that
existed on the entry's publication date. An organisation created after that
date signed up against a register that already listed the change. There is no
separate privacy/DPO contact on an organisation yet (tracked in
`docs/followups.md`), so the administrators are the contact the platform holds;
the published DPA says exactly that.

The notice is **contractual, not a product notification**: it is sent straight
through the email adapter, never through ``notification_dispatch`` — whose
per-user preferences could switch it off — and it is in English, the language
of the DPA it is given under, whatever the recipient's email locale. It goes
out unbranded: the processor giving notice is the platform, not a reseller's
white-label.

Idempotent and auditable
------------------------
Each (entry, organisation) moves through the ``subprocessor_notices`` ledger in
short transactions, so no lock is ever held across an email send or a tenant
round trip (the ``post_commit`` rule):

1. **Claim** — insert the UNIQUE pair ``ON CONFLICT DO NOTHING`` and commit. A
   second replica or a re-run finds the row and sends nothing. A claim with no
   ``sent_at`` older than :data:`CLAIM_TIMEOUT` belongs to a run that died
   mid-send and is re-taken — the one path that can email an organisation twice
   (at-least-once on a crash: a duplicate notice is recoverable, a missing one
   is the defect this closes).
2. **Send** — to each active administrator. If nobody could be emailed the claim
   is deleted and the next tick retries; otherwise ``sent_at``, the counts, the
   recipient user ids and ``late`` are committed, and the emails are never
   re-sent from here on. An organisation with **no active administrator** is
   not a failure of the sweep — there is nobody it could have told — so it is
   counted apart (``organizations_without_admin``), its claim released, and it
   stays owed until it has one (`docs/followups.md`).
3. **Audit** — the tenant's ``privacy.subprocessor_notice_sent`` row, then
   ``audited_at``. A failed audit write counts as a tick failure and is retried
   on its own every tick until it lands; it never re-sends the notice. The row
   is keyed by ``correlation_id`` = the notice id and written only if absent, so
   a stamp that failed after the row committed never appends a second one.

The 30 days are measured, not assumed: each row stores the entry's effective
date and ``late`` — true when the notice reached the organisation with fewer
than :data:`NOTICE_PERIOD_DAYS` days to go (an urgent entry, or a sweep that was
switched off). The tick reports late notices as a counter, so ``GET
/api/health/sweeps`` shows them.

Disabled by default (``FEOH_SUBPROCESSOR_NOTICES_ENABLED``) like every sweep
that emails; a deployed environment refuses to boot with it off
(``config._require_subprocessor_notices_in_deployed_envs``). Never moves money.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import control_session_factory
from app.models.organization import Organization
from app.models.subprocessor_notice import SubprocessorNotice
from app.models.user import Role, User, UserRole
from app.models.workflow import AuditLog
from app.services.audit import log_action
from app.services.email_adapters import get_email_adapter
from app.services.email_adapters.base import EmailMessage
from app.services.subprocessor_changelog import (
    CHANGELOG,
    NOTICE_PERIOD_DAYS,
    OBJECTION_CONTACT,
    ChangelogEntry,
    long_date,
    notice_entries,
)
from app.services.sweep_health import SWEEP_SUBPROCESSOR_NOTICES, run_sweep_loop

logger = logging.getLogger(__name__)

ADMIN_ROLE = "admin"
AUDIT_ACTION = "privacy.subprocessor_notice_sent"
#: A claim this old with nothing sent belongs to a run that died mid-send.
CLAIM_TIMEOUT = timedelta(hours=1)


class NoNoticeDelivered(RuntimeError):
    """No administrator of the organisation could be sent the notice."""


class NoActiveAdministrator(NoNoticeDelivered):
    """The organisation has nobody the notice could go to. Not a send failure:
    counted as ``organizations_without_admin``, never as a tick failure."""


@dataclass
class NoticeResult:
    """Per-tick outcome; field names are the health payload's counters."""

    entries_due: int = 0
    organizations_notified: int = 0
    emails_sent: int = 0
    late_notices: int = 0
    #: Administrators the provider refused on a notice that otherwise went out.
    failed_recipients: int = 0
    #: Owed organisations with no active administrator to tell (still owed).
    organizations_without_admin: int = 0
    audits_written: int = 0
    failures: int = 0


@dataclass
class _Recipient:
    user_id: uuid.UUID
    email: str
    full_name: str


@dataclass
class PartialDelivery:
    """A sent notice that some of the organisation's administrators missed."""

    entry_id: str
    organization_id: uuid.UUID
    sent_at: datetime
    recipients: int
    failed_recipients: int


@dataclass
class OwedNotice:
    """One entry and the organisations still owed its notice (dry run)."""

    entry: ChangelogEntry
    organization_ids: list[uuid.UUID] = field(default_factory=list)


def _published_cutoff(entry: ChangelogEntry) -> datetime:
    """Organisations created before the end of the publication day are told."""
    return datetime.combine(entry.published_on + timedelta(days=1), time.min, tzinfo=UTC)


def is_late(entry: ChangelogEntry, sent_on: date) -> bool:
    """Did this notice leave fewer than the promised days before the change?"""
    assert entry.effective_on is not None
    return (entry.effective_on - sent_on).days < NOTICE_PERIOD_DAYS


def register_url() -> str:
    return f"{settings.public_url.rstrip('/')}/legal/sub-processors#change-log"


def dpa_url() -> str:
    return f"{settings.public_url.rstrip('/')}/legal/dpa#sub-processor-changes"


def render_notice(
    entry: ChangelogEntry, *, org_name: str, recipient_name: str, sent_on: date
) -> tuple[str, str]:
    """(subject, plaintext body) for one recipient. English by design — see
    the module docstring. Carries the org name and the change only; nothing
    about the recipient beyond the greeting name."""
    assert entry.effective_on is not None
    effective = long_date(entry.effective_on)
    subject = f"Notice of a sub-processor change for {org_name}, effective {effective}"
    lines = [
        f"Hi {recipient_name},",
        "",
        f"You are receiving this as an administrator of the {org_name} workspace on "
        "FeohLedger. Our Data Processing Addendum promises your organisation notice "
        "before we engage a new sub-processor, replace one, or change what one "
        "receives. This is that notice.",
        "",
        f"Change: {entry.change}",
        f"Published: {long_date(entry.published_on)}",
        # A late notice (the sweep was off, or an urgent entry) can go out on
        # or after the day the change took effect; say so rather than claim
        # a future date.
        f"Took effect: {effective}"
        if entry.effective_on <= sent_on
        else f"Takes effect: {effective}",
    ]
    if entry.urgent_reason:
        lines += [
            "",
            f"This change is being made on less than {NOTICE_PERIOD_DAYS} days' notice, "
            f"for this reason: {entry.urgent_reason}",
        ]
    lines += [
        "",
        f"The full register and its dated change log: {register_url()}",
        "",
        "You may object on reasonable data-protection grounds within "
        f"{NOTICE_PERIOD_DAYS} days of this notice by writing to {OBJECTION_CONTACT} "
        "and stating your grounds. What happens next is set out in the Data "
        f"Processing Addendum: {dpa_url()}",
        "",
        "This is a contractual notice, not a product notification: it goes to the active "
        "administrators of your workspace and cannot be turned off in notification "
        "settings.",
    ]
    return subject, "\n".join(lines)


async def _admins(control_db: AsyncSession, org_id: uuid.UUID) -> list[_Recipient]:
    rows = (
        await control_db.execute(
            select(User.id, User.email, User.full_name)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                User.organization_id == org_id,
                User.is_active.is_(True),
                Role.name == ADMIN_ROLE,
                # The system role only: a custom role that happens to be named
                # "admin" is not the organisation's administrator.
                Role.organization_id.is_(None),
            )
            .order_by(User.created_at, User.id)
        )
    ).all()
    return [_Recipient(user_id=r[0], email=r[1], full_name=r[2]) for r in rows]


def _stale_claim(now: datetime):
    return (SubprocessorNotice.sent_at.is_(None)) & (
        SubprocessorNotice.claimed_at < now - CLAIM_TIMEOUT
    )


async def owed_notices(
    control_db: AsyncSession,
    *,
    now: datetime,
    entries: tuple[ChangelogEntry, ...] = CHANGELOG,
) -> list[OwedNotice]:
    """Every published notice entry with the organisations not yet told —
    no ledger row, or only a claim abandoned mid-send."""
    owed: list[OwedNotice] = []
    for entry in notice_entries(now.date(), entries):
        settled = select(SubprocessorNotice.organization_id).where(
            SubprocessorNotice.entry_id == entry.id, ~_stale_claim(now)
        )
        org_ids = (
            (
                await control_db.execute(
                    select(Organization.id)
                    .where(
                        Organization.created_at < _published_cutoff(entry),
                        Organization.id.not_in(settled),
                    )
                    .order_by(Organization.id)
                )
            )
            .scalars()
            .all()
        )
        owed.append(OwedNotice(entry=entry, organization_ids=list(org_ids)))
    return owed


async def _claim(
    control_db: AsyncSession, entry: ChangelogEntry, org_id: uuid.UUID, *, now: datetime
) -> uuid.UUID | None:
    claimed = (
        await control_db.execute(
            pg_insert(SubprocessorNotice)
            .values(
                id=uuid.uuid4(),
                entry_id=entry.id,
                organization_id=org_id,
                claimed_at=now,
                effective_on=entry.effective_on,
                recipients=0,
                failed_recipients=0,
                late=False,
            )
            .on_conflict_do_nothing(constraint="uq_subprocessor_notices_entry_org")
            .returning(SubprocessorNotice.id)
        )
    ).scalar_one_or_none()
    if claimed is not None:
        return claimed
    # Re-take a claim whose run died before anything was recorded as sent. The
    # WHERE re-checks staleness under the row lock, so two runs cannot both win.
    return (
        await control_db.execute(
            update(SubprocessorNotice)
            .where(
                SubprocessorNotice.entry_id == entry.id,
                SubprocessorNotice.organization_id == org_id,
                _stale_claim(now),
            )
            .values(claimed_at=now)
            .returning(SubprocessorNotice.id)
        )
    ).scalar_one_or_none()


async def _write_audit(control_db: AsyncSession, notice_id: uuid.UUID, *, now: datetime) -> bool:
    """Write the tenant audit row for a SENT notice, then stamp ``audited_at``.

    Raises on failure (the ``dunning_sweep`` rule); the notice stays sent and
    only the audit is retried. False when there is nothing to do (already
    audited, or the organisation is gone with its tenant).

    **Exactly one row per notice.** The tenant row and the control-plane stamp
    live in two databases, so a stamp that fails after the tenant commit leaves
    the row written and ``audited_at`` NULL, and the next tick comes back. The
    row is therefore written only when the tenant holds no ``AUDIT_ACTION`` row
    with ``correlation_id`` = the notice id — checked and inserted in one tenant
    transaction under an advisory lock on the notice id, so two replicas
    retrying the same stamp cannot both insert.

    That check needs the row to exist once the transaction commits, so it is
    written **synchronously in every audit mode**, as
    ``audit_dispatch.dispatch_durable_auth_audit`` does: in ``lambda`` mode the
    ordinary path only enqueues to SQS, and a message still in flight is
    invisible to the check (a retry would append a second row once the Lambda
    caught up). It is the same ``log_action`` row the Lambda would write.
    """
    # Resolved at call time: tests and `dispatch_engine_scope` swap it. The
    # same per-organisation tenant-engine shape as `dunning_sweep` and
    # `audit_dispatch._write_auth_audit`: a control-plane sweep has no request,
    # so no `X-Tenant-Slug` for `get_tenant_db` to resolve.
    from app.database import get_tenant_engine

    row = (
        await control_db.execute(
            select(SubprocessorNotice, Organization.db_name)
            .join(Organization, Organization.id == SubprocessorNotice.organization_id)
            .where(
                SubprocessorNotice.id == notice_id,
                SubprocessorNotice.sent_at.is_not(None),
                SubprocessorNotice.audited_at.is_(None),
            )
        )
    ).first()
    if row is None:
        await control_db.rollback()
        return False
    notice, db_name = row
    entry = next((e for e in CHANGELOG if e.id == notice.entry_id), None)
    sent_on = notice.sent_at.date()
    details = {
        "entry_id": notice.entry_id,
        "effective_on": notice.effective_on.isoformat(),
        "sent_on": sent_on.isoformat(),
        "notice_days": (notice.effective_on - sent_on).days,
        "late": notice.late,
        # User ids, never addresses (PII-out-of-logs).
        "recipient_user_ids": list(notice.recipient_user_ids or []),
        "failed_recipients": notice.failed_recipients,
    }
    if entry is not None:
        details["published_on"] = entry.published_on.isoformat()
        details["urgent"] = bool(entry.urgent_reason)
    organization_id = notice.organization_id
    await control_db.rollback()
    factory = async_sessionmaker(get_tenant_engine(db_name), expire_on_commit=False)
    async with factory() as tenant_db:
        try:
            await tenant_db.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(str(notice_id), 0)))
            )
            already = (
                await tenant_db.execute(
                    select(AuditLog.id)
                    .where(AuditLog.action == AUDIT_ACTION, AuditLog.correlation_id == notice_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if already is None:
                await log_action(
                    tenant_db,
                    correlation_id=notice_id,
                    organization_id=organization_id,
                    actor_id=None,  # the platform giving notice, no human actor
                    action=AUDIT_ACTION,
                    entity_type="organization",
                    entity_id=organization_id,
                    details=details,
                )
            await tenant_db.commit()
        except BaseException:
            await tenant_db.rollback()
            raise
    await control_db.execute(
        update(SubprocessorNotice).where(SubprocessorNotice.id == notice_id).values(audited_at=now)
    )
    await control_db.commit()
    return True


async def partial_deliveries(control_db: AsyncSession) -> list[PartialDelivery]:
    """Sent notices some administrators did not receive: the operator's view
    (``scripts/subprocessor_notices.py list``). Ids and counts only."""
    rows = (
        await control_db.execute(
            select(
                SubprocessorNotice.entry_id,
                SubprocessorNotice.organization_id,
                SubprocessorNotice.sent_at,
                SubprocessorNotice.recipients,
                SubprocessorNotice.failed_recipients,
            )
            .where(
                SubprocessorNotice.sent_at.is_not(None),
                SubprocessorNotice.failed_recipients > 0,
            )
            .order_by(SubprocessorNotice.entry_id, SubprocessorNotice.sent_at)
        )
    ).all()
    return [PartialDelivery(*r) for r in rows]


async def notify_organization(
    control_db: AsyncSession,
    entry: ChangelogEntry,
    org_id: uuid.UUID,
    *,
    now: datetime,
) -> int | None:
    """Notify one organisation of one entry, once (module docstring, steps 1-2).

    Returns the number of administrators emailed, or ``None`` when another run
    holds or finished this (entry, organisation). Raises
    :class:`NoNoticeDelivered` when nobody could be told — the claim is removed
    so the next tick retries. Does not write the audit row; see
    :func:`_write_audit`.
    """
    assert entry.effective_on is not None
    try:
        claim_id = await _claim(control_db, entry, org_id, now=now)
        if claim_id is None:
            await control_db.rollback()
            return None
        org = await control_db.get(Organization, org_id)
        recipients = await _admins(control_db, org_id) if org is not None else []
        if not recipients:
            # Not yet committed (or a re-take, released again): nothing to undo.
            await control_db.rollback()
            if org is None:
                return None
            raise NoActiveAdministrator("organisation has no active administrator")
        org_name = org.name
        # The claim is durable before any email leaves; no lock is held while
        # the provider is called.
        await control_db.commit()
    except BaseException:
        await control_db.rollback()
        raise

    adapter = get_email_adapter()
    sent_on = now.date()
    delivered: list[uuid.UUID] = []
    failed = 0
    for person in recipients:
        subject, body = render_notice(
            entry, org_name=org_name, recipient_name=person.full_name, sent_on=sent_on
        )
        try:
            await adapter.send(
                EmailMessage(
                    to=person.email,
                    subject=subject,
                    body_text=body,
                    links=(register_url(), dpa_url()),
                    preheader="Advance notice of a sub-processor change, and how to object.",
                )
            )
            delivered.append(person.user_id)
        except Exception as exc:  # noqa: BLE001 — one address must not stop the rest
            failed += 1
            logger.warning(
                "[subprocessor-notices] entry=%s org=%s user=%s send failed: %s",
                entry.id,
                org_id,
                person.user_id,
                exc.__class__.__name__,
            )

    try:
        if not delivered:
            await control_db.execute(
                delete(SubprocessorNotice).where(
                    SubprocessorNotice.id == claim_id, SubprocessorNotice.sent_at.is_(None)
                )
            )
            await control_db.commit()
            raise NoNoticeDelivered("no administrator's email could be sent")
        await control_db.execute(
            update(SubprocessorNotice)
            .where(SubprocessorNotice.id == claim_id)
            .values(
                sent_at=now,
                recipients=len(delivered),
                failed_recipients=failed,
                recipient_user_ids=[str(u) for u in delivered],
                late=is_late(entry, sent_on),
            )
        )
        await control_db.commit()
    except BaseException:
        await control_db.rollback()
        raise
    return len(delivered)


async def _unaudited(control_db: AsyncSession) -> list[uuid.UUID]:
    ids = (
        (
            await control_db.execute(
                select(SubprocessorNotice.id)
                .join(Organization, Organization.id == SubprocessorNotice.organization_id)
                .where(
                    SubprocessorNotice.sent_at.is_not(None),
                    SubprocessorNotice.audited_at.is_(None),
                )
                .order_by(SubprocessorNotice.sent_at)
            )
        )
        .scalars()
        .all()
    )
    await control_db.rollback()
    return list(ids)


async def _audit(
    control_db: AsyncSession, notice_id: uuid.UUID, *, now: datetime, result: NoticeResult
) -> None:
    try:
        if await _write_audit(control_db, notice_id, now=now):
            result.audits_written += 1
    except Exception as exc:  # noqa: BLE001 — retried next tick, never re-sent
        await control_db.rollback()
        result.failures += 1
        logger.warning(
            "[subprocessor-notices] notice=%s audit write failed: %s",
            notice_id,
            exc.__class__.__name__,
        )


async def run_notices_once(
    control_db: AsyncSession,
    *,
    now: datetime | None = None,
    entries: tuple[ChangelogEntry, ...] = CHANGELOG,
) -> NoticeResult:
    """Send every notice that is owed and write every audit row still missing.
    One organisation's failure costs one."""
    now = now or datetime.now(UTC)
    result = NoticeResult()
    # Notices that went out on an earlier tick whose audit row did not land.
    for notice_id in await _unaudited(control_db):
        await _audit(control_db, notice_id, now=now, result=result)

    owed = await owed_notices(control_db, now=now, entries=entries)
    # End the read transaction before the per-organisation legs begin.
    await control_db.rollback()
    for item in owed:
        if item.organization_ids:
            result.entries_due += 1
        for org_id in item.organization_ids:
            try:
                sent = await notify_organization(control_db, item.entry, org_id, now=now)
            except NoActiveAdministrator:
                # Nobody to tell is not a send failure: reported apart, so the
                # sweep stays green while the gap shows on /api/health/sweeps.
                result.organizations_without_admin += 1
                continue
            except Exception as exc:  # noqa: BLE001 — one organisation must not halt the tick
                result.failures += 1
                logger.warning(
                    "[subprocessor-notices] entry=%s org=%s notice failed: %s",
                    item.entry.id,
                    org_id,
                    exc.__class__.__name__,
                )
                continue
            if sent is None:
                continue
            result.organizations_notified += 1
            result.emails_sent += sent
            if is_late(item.entry, now.date()):
                result.late_notices += 1
            notice_id, failed = (
                await control_db.execute(
                    select(SubprocessorNotice.id, SubprocessorNotice.failed_recipients).where(
                        SubprocessorNotice.entry_id == item.entry.id,
                        SubprocessorNotice.organization_id == org_id,
                    )
                )
            ).one()
            await control_db.rollback()
            result.failed_recipients += failed
            await _audit(control_db, notice_id, now=now, result=result)
    return result


async def _notices_tick() -> NoticeResult:
    async with control_session_factory() as control_db:
        result = await run_notices_once(control_db)
    if result.organizations_notified or result.failures or result.organizations_without_admin:
        logger.info(
            "[subprocessor-notices] notified %d organisation(s), %d email(s), late=%d, "
            "failed_recipients=%d, without_admin=%d, failed=%d",
            result.organizations_notified,
            result.emails_sent,
            result.late_notices,
            result.failed_recipients,
            result.organizations_without_admin,
            result.failures,
        )
    return result


async def run_subprocessor_notices_loop() -> None:
    """Long-lived sweep loop. Started in ``main.lifespan`` when enabled."""
    await run_sweep_loop(
        SWEEP_SUBPROCESSOR_NOTICES,
        lambda: _notices_tick(),
        interval_seconds=settings.subprocessor_notices_interval_seconds,
        log=logger,
        log_prefix="[subprocessor-notices]",
    )
