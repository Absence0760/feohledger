"""The last-active-admin guard, shared by every path that can take `admin` away.

An organization with zero active holders of the system ``admin`` role cannot
recover on its own: granting a role and reactivating an account both need
``user.manage``, which by default only an admin holds. So every write that can
remove an active admin — the admin API's PATCH and deletes, and the SCIM
deactivation and group-role reconciliation an IdP drives — asks this module
first, and each surface turns the refusal into its own error shape (a 409 on
``/api/admin``, a SCIM ``400 mutability`` on ``/api/scim/v2``).

**The check is serialized, not just made.** Counting the other admins and then
acting is a check-then-act: two admins demoting each other at the same moment
each saw the other still standing, both passed, and the org was left with none.
Before it counts, the guard takes the organization row with ``SELECT … FOR NO
KEY UPDATE``, held to the caller's commit, so a second removal in the same org
waits for the first to commit and then counts what it left behind. It is the
same row ``tenant.lock_organization`` serializes the settings writers on (``FOR
UPDATE`` conflicts with ``FOR NO KEY UPDATE``); the weaker mode is chosen so an
unrelated insert of a ``users`` row in the org, whose foreign key takes ``FOR
KEY SHARE`` on that row, is not made to wait behind it. The lock is taken only
when the write would actually remove an active admin, so ordinary edits never
touch it. ``docs/decisions.md`` §290.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN
from app.models.organization import Organization
from app.models.user import Role, User, UserRole

LAST_ADMIN_MESSAGE = (
    "Cannot remove the organization's last active admin. Promote another user to admin first."
)


class LastAdminLockout(Exception):
    """The write would leave the organization with no active admin."""

    def __init__(self) -> None:
        super().__init__(LAST_ADMIN_MESSAGE)


def removes_active_admin(
    *,
    was_active: bool,
    previous_role_names: Iterable[str],
    new_is_active: bool,
    new_role_names: Iterable[str],
) -> bool:
    """Is the target an active admin now, and would the write make them not one?"""
    is_admin_now = was_active and ROLE_ADMIN in set(previous_role_names)
    stays_admin = new_is_active and ROLE_ADMIN in set(new_role_names)
    return is_admin_now and not stays_admin


async def lock_admin_set(db: AsyncSession, org_id: uuid.UUID) -> None:
    """Serialize every last-admin decision in this org until the caller commits."""
    await db.execute(
        select(Organization.id).where(Organization.id == org_id).with_for_update(key_share=True)
    )


async def org_has_active_admin_outside(
    db: AsyncSession, org_id: uuid.UUID, *, excluded_user_ids: Iterable[uuid.UUID]
) -> bool:
    """Does the org have an active system-``admin`` holder not in ``excluded_user_ids``?"""
    excluded = list(excluded_user_ids)
    stmt = (
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.organization_id == org_id,
            User.is_active.is_(True),
            Role.name == ROLE_ADMIN,
            Role.organization_id.is_(None),
        )
    )
    if excluded:
        stmt = stmt.where(User.id.not_in(excluded))
    return ((await db.execute(stmt)).scalar() or 0) > 0


async def guard_admin_removal(
    db: AsyncSession, org_id: uuid.UUID, *, removed_user_ids: Iterable[uuid.UUID]
) -> None:
    """Refuse removing admin from every user in ``removed_user_ids`` at once when no
    active admin would remain. The callers have already established that each of
    them is an active admin the write would demote, deactivate or delete; an
    empty set is a no-op and takes no lock."""
    removed = list(removed_user_ids)
    if not removed:
        return
    await lock_admin_set(db, org_id)
    if not await org_has_active_admin_outside(db, org_id, excluded_user_ids=removed):
        raise LastAdminLockout()


async def guard_against_last_admin_lockout(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    target_id: uuid.UUID,
    was_active: bool,
    previous_role_names: Iterable[str],
    new_is_active: bool,
    new_role_names: Iterable[str],
) -> None:
    """Raise :class:`LastAdminLockout` when this one-user write would leave the
    org with zero active admins.

    Fires whenever the target is CURRENTLY an active admin and the write would
    make them not one — demoted, deactivated, deleted, or several at once —
    self or not. Self-mutation is the case a caller-outranks-target check never
    catches, and a third-party removal is the case only the lock catches: the
    caller is an admin when the request starts, but a concurrent request can be
    removing them at the same moment.

    Must run before the write is applied to the session, and on the session that
    will commit it — the lock it takes is held to that commit.
    """
    if removes_active_admin(
        was_active=was_active,
        previous_role_names=previous_role_names,
        new_is_active=new_is_active,
        new_role_names=new_role_names,
    ):
        await guard_admin_removal(db, org_id, removed_user_ids=[target_id])


async def role_names_for(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """The names of the roles a user holds, for callers that did not eager-load them."""
    rows = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return sorted(rows.scalars().all())
