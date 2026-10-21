"""The last-active-admin guard (`services/admin_lockout`) and its serialization.

The guard used to count the org's other active admins and then act, with
nothing held in between. Two admins demoting each other at the same moment each
counted the other as still standing, both passed, and the org was left with no
admin — the one outcome the guard exists to prevent. The guard now takes the
organization row before it counts, so the second removal waits for the first
to commit and then counts what the first left behind.

The race is driven for real against Postgres: two control-plane sessions, the
first holding its demotion uncommitted while the second reaches the guard. The
test waits on the real signal — `pg_stat_activity` showing a backend blocked on
a lock — never on a sleep.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, text

from app.models.user import Role, User, UserRole
from app.services.admin_lockout import (
    LastAdminLockout,
    guard_against_last_admin_lockout,
    removes_active_admin,
)

# --- the pure rule ------------------------------------------------------------


@pytest.mark.parametrize(
    ("was_active", "before", "new_active", "after", "expected"),
    [
        (True, ["admin"], True, [], True),  # demoted
        (True, ["admin"], False, ["admin"], True),  # deactivated
        (True, ["admin", "cfo"], False, [], True),  # both
        (True, ["admin"], True, ["admin", "cfo"], False),  # keeps admin
        (False, ["admin"], False, [], False),  # was not an ACTIVE admin
        (True, ["cfo"], False, [], False),  # was never an admin
    ],
)
def test_removes_active_admin(was_active, before, new_active, after, expected):
    assert (
        removes_active_admin(
            was_active=was_active,
            previous_role_names=before,
            new_is_active=new_active,
            new_role_names=after,
        )
        is expected
    )


# --- the race -----------------------------------------------------------------


async def _admin_role_id(mk) -> uuid.UUID:
    async with mk() as s:
        return (
            await s.execute(
                select(Role.id).where(Role.name == "admin", Role.organization_id.is_(None))
            )
        ).scalar_one()


async def _blocked_on_a_lock(mk, pid: int, *, timeout_s: float = 10.0) -> bool:
    """Poll `pg_stat_activity` until backend `pid` is waiting on a lock."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).scalar_one_or_none()
        if waiting == "Lock":
            return True
        await asyncio.sleep(0.02)
    return False


def _demotion(org_id, target_id):
    return {
        "org_id": org_id,
        "target_id": target_id,
        "was_active": True,
        "previous_role_names": ["admin"],
        "new_is_active": True,
        "new_role_names": [],
    }


@pytest.mark.asyncio
async def test_two_admins_demoting_each_other_cannot_both_succeed(realdb):
    """A demotes B while B demotes A. Exactly one wins; the org keeps an admin."""
    mk = realdb.control_sessionmaker()
    info = realdb.info("a")
    org_id, a_id = info.org_id, info.users["admin"]
    admin_role_id = await _admin_role_id(mk)

    b_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            User(
                id=b_id,
                email=f"race-admin-{b_id}@acme.test",
                full_name="Race Admin",
                hashed_password=None,
                organization_id=org_id,
                is_active=True,
                must_change_password=False,
            )
        )
        await s.flush()
        s.add(UserRole(user_id=b_id, role_id=admin_role_id))
        await s.commit()

    try:
        async with mk() as first, mk() as second:
            # First: A demotes B. The guard passes (A is still an admin) and the
            # demotion is applied but NOT committed — the window the old
            # unlocked count raced in.
            await guard_against_last_admin_lockout(first, **_demotion(org_id, b_id))
            await first.execute(delete(UserRole).where(UserRole.user_id == b_id))

            # Second, concurrently: B demotes A. Unlocked, it would count B as
            # an active admin (the first demotion is not visible yet) and pass.
            pid = (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            racing = asyncio.create_task(
                guard_against_last_admin_lockout(second, **_demotion(org_id, a_id))
            )
            assert await _blocked_on_a_lock(mk, pid), "the second guard never waited"
            assert not racing.done()

            await first.commit()
            # Released, it counts what the first left behind: no admin but A.
            with pytest.raises(LastAdminLockout):
                await racing
            await second.rollback()

        async with mk() as s:
            a_roles = (
                (
                    await s.execute(
                        select(Role.name)
                        .join(UserRole, UserRole.role_id == Role.id)
                        .where(UserRole.user_id == a_id)
                    )
                )
                .scalars()
                .all()
            )
        assert "admin" in a_roles
    finally:
        async with mk() as s:
            await s.execute(delete(UserRole).where(UserRole.user_id == b_id))
            await s.execute(delete(User).where(User.id == b_id))
            await s.commit()


@pytest.mark.asyncio
async def test_an_ordinary_edit_takes_no_lock(realdb):
    """The org row is locked only when the write would remove an active admin,
    so a role change on a non-admin never queues behind a settings writer."""
    mk = realdb.control_sessionmaker()
    info = realdb.info("a")
    async with mk() as holder, mk() as other:
        # Hold the org row the way a settings writer does.
        await holder.execute(
            text("SELECT id FROM organizations WHERE id = :id FOR UPDATE"), {"id": info.org_id}
        )
        # Would block (and time out the test) if it tried to take the row.
        await asyncio.wait_for(
            guard_against_last_admin_lockout(
                other,
                org_id=info.org_id,
                target_id=info.users["ap_clerk"],
                was_active=True,
                previous_role_names=["ap_clerk"],
                new_is_active=False,
                new_role_names=[],
            ),
            timeout=5,
        )
        await holder.rollback()
