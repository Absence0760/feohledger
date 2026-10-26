"""Send the DPA's sub-processor change notice to every customer organisation.

Issue #427 / `docs/decisions.md` §320. `/legal/dpa` § 8 promises at least 30
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
Each (entry, organisation) is claimed in ``subprocessor_notices`` (UNIQUE pair)
*before* any email is sent, in the same transaction that later records the
outcome, so two replicas cannot both notify one organisation and a re-run
skips it. The organisation is counted notified when at least one administrator's
email was handed to the provider; if none was, the claim rolls back and the
next tick retries. A tenant audit row (``privacy.subprocessor_notice_sent``) is
written before the control-plane commit and RAISES on failure (the
``dunning_sweep`` rule): a notice is never recorded without its trail.

Delivery is at-least-once, not exactly-once: a crash between the send and the
commit re-sends on the next tick. A duplicate notice is recoverable; a missing
one is the defect this exists to close.

The 30 days are measured, not assumed: each row stores the entry's effective
date and ``late`` — true when the notice reached the organisation with fewer
than :data:`NOTICE_PERIOD_DAYS` days to go (an urgent entry, or a sweep that was
switched off). The tick reports late notices as a counter, so ``GET
/api/health/sweeps`` shows them.

Disabled by default (``FEOH_SUBPROCESSOR_NOTICES_ENABLED``) like every sweep
that emails; a deployment with customers must turn it on. Never moves money.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import control_session_factory
from app.models.organization import Organization
from app.models.subprocessor_notice import SubprocessorNotice
from app.models.user import Role, User, UserRole
from app.services.audit_dispatch import dispatch_audit
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


class NoNoticeDelivered(RuntimeError):
    """No administrator of the organisation could be sent the notice."""


@dataclass
class NoticeResult:
    """Per-tick outcome; field names are the health payload's counters."""

    entries_due: int = 0
    organizations_notified: int = 0
    emails_sent: int = 0
    late_notices: int = 0
    failures: int = 0


@dataclass
class _Recipient:
    user_id: uuid.UUID
    email: str
    full_name: str


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


def render_notice(entry: ChangelogEntry, *, org_name: str, recipient_name: str) -> tuple[str, str]:
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
        f"Takes effect: {effective}",
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
        "This is a contractual notice, not a product notification: it goes to every "
        "administrator of your workspace and cannot be turned off in notification "
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


async def owed_notices(
    control_db: AsyncSession,
    *,
    today: date,
    entries: tuple[ChangelogEntry, ...] = CHANGELOG,
) -> list[OwedNotice]:
    """Every published notice entry with the organisations not yet told."""
    owed: list[OwedNotice] = []
    for entry in notice_entries(today, entries):
        already = select(SubprocessorNotice.organization_id).where(
            SubprocessorNotice.entry_id == entry.id
        )
        org_ids = (
            (
                await control_db.execute(
                    select(Organization.id)
                    .where(
                        Organization.created_at < _published_cutoff(entry),
                        Organization.id.not_in(already),
                    )
                    .order_by(Organization.id)
                )
            )
            .scalars()
            .all()
        )
        owed.append(OwedNotice(entry=entry, organization_ids=list(org_ids)))
    return owed


async def _record_audit(
    *,
    org: Organization,
    entry: ChangelogEntry,
    sent_on: date,
    late: bool,
    delivered: list[uuid.UUID],
    failed: int,
) -> None:
    """The tenant's own trail of the notice. Raises — see the module docstring."""
    # Resolved at call time: tests and `dispatch_engine_scope` swap it.
    from app.database import get_tenant_engine

    assert entry.effective_on is not None
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    async with factory() as tenant_db:
        await dispatch_audit(
            tenant_db,
            correlation_id=uuid.uuid4(),
            organization_id=org.id,
            actor_id=None,  # the platform giving notice, no human actor
            action=AUDIT_ACTION,
            entity_type="organization",
            entity_id=org.id,
            details={
                "entry_id": entry.id,
                "published_on": entry.published_on.isoformat(),
                "effective_on": entry.effective_on.isoformat(),
                "sent_on": sent_on.isoformat(),
                "notice_days": (entry.effective_on - sent_on).days,
                "late": late,
                "urgent": bool(entry.urgent_reason),
                # User ids, never addresses (PII-out-of-logs).
                "recipient_user_ids": [str(u) for u in delivered],
                "failed_recipients": failed,
            },
        )
        await tenant_db.commit()


async def notify_organization(
    control_db: AsyncSession,
    entry: ChangelogEntry,
    org_id: uuid.UUID,
    *,
    now: datetime,
) -> int | None:
    """Notify one organisation of one entry, exactly once.

    Returns the number of administrators emailed, or ``None`` when another
    run already claimed this (entry, organisation). Raises when nobody could be
    told; the claim is rolled back so the next tick retries.
    """
    assert entry.effective_on is not None
    sent_on = now.date()
    late = is_late(entry, sent_on)
    try:
        claimed = (
            await control_db.execute(
                pg_insert(SubprocessorNotice)
                .values(
                    id=uuid.uuid4(),
                    entry_id=entry.id,
                    organization_id=org_id,
                    sent_at=now,
                    effective_on=entry.effective_on,
                    recipients=0,
                    failed_recipients=0,
                    late=late,
                )
                .on_conflict_do_nothing(constraint="uq_subprocessor_notices_entry_org")
                .returning(SubprocessorNotice.id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            await control_db.rollback()
            return None

        org = await control_db.get(Organization, org_id)
        if org is None:  # deleted between the listing and the claim
            await control_db.rollback()
            return None
        recipients = await _admins(control_db, org_id)
        if not recipients:
            raise NoNoticeDelivered("organisation has no active administrator")

        adapter = get_email_adapter()
        delivered: list[uuid.UUID] = []
        failed = 0
        for person in recipients:
            subject, body = render_notice(entry, org_name=org.name, recipient_name=person.full_name)
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
        if not delivered:
            raise NoNoticeDelivered("no administrator's email could be sent")

        await control_db.execute(
            update(SubprocessorNotice)
            .where(SubprocessorNotice.id == claimed)
            .values(recipients=len(delivered), failed_recipients=failed)
        )
        await _record_audit(
            org=org, entry=entry, sent_on=sent_on, late=late, delivered=delivered, failed=failed
        )
        await control_db.commit()
    except BaseException:
        await control_db.rollback()
        raise
    return len(delivered)


async def run_notices_once(
    control_db: AsyncSession,
    *,
    now: datetime | None = None,
    entries: tuple[ChangelogEntry, ...] = CHANGELOG,
) -> NoticeResult:
    """Send every notice that is owed. One organisation's failure costs one."""
    now = now or datetime.now(UTC)
    result = NoticeResult()
    owed = await owed_notices(control_db, today=now.date(), entries=entries)
    # End the read transaction before the per-organisation legs begin.
    await control_db.rollback()
    for item in owed:
        if item.organization_ids:
            result.entries_due += 1
        for org_id in item.organization_ids:
            try:
                sent = await notify_organization(control_db, item.entry, org_id, now=now)
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
    return result


async def _notices_tick() -> NoticeResult:
    async with control_session_factory() as control_db:
        result = await run_notices_once(control_db)
    if result.organizations_notified or result.failures:
        logger.info(
            "[subprocessor-notices] notified %d organisation(s), %d email(s), late=%d, failed=%d",
            result.organizations_notified,
            result.emails_sent,
            result.late_notices,
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
