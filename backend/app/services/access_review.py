"""Periodic access reviews — flag users holding unused elevated permissions (SOX).

A SOX access-control requirement is to *review* who holds privileged access and
prove that access is still used. This service is the read side of that review:
it lists every user in the org who holds an elevated role **or an elevated
granular permission** and, for each, derives their "last privileged action"
from the tenant ``audit_log`` (the append-only, WORM-shipped trail every
mutation already writes). A user whose last privileged action is older than the
dormancy window — or who has never acted — is flagged DORMANT so a reviewer can
decide whether to revoke the access.

"Elevated" is not just the three system role names (``admin`` / ``ap_manager``
/ ``cfo``): a custom role (`app/api/permissions.py`) can grant a fraud-sensitive
permission like ``payment.execute`` to a role named anything at all, and that
user must be just as visible to this review — see ``ELEVATED_PERMISSIONS``.

Compute-on-read: there is **no** ``last_elevated_use`` column and no migration.
The index is derived live by aggregating ``MAX(audit_log.created_at)`` per actor.
This keeps the trail itself the single source of truth (it can't drift from a
denormalised column) and inherits the audit table's immutability for free.

Pure-ish + deterministic: the only inputs are the two DB sessions and "now".
No LLM, no network. The HTTP route (``app/api/access_reviews.py``) owns the
sensitive-read audit row and the RBAC gate.

Scope of "privileged action": we count *mutating* audit rows only — a
``*.viewed`` row means the user merely looked at a record, which is not evidence
that their elevated *write* permission is still needed. So a CFO who only ever
opens the dashboard (all ``*.viewed`` reads) is correctly surfaced as dormant
for their elevated mutate rights. Read rows are excluded by dropping any action
ending in ``.viewed`` / ``.exported`` (both are access-log verbs, not mutations).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO
from app.api.permissions import (
    PERM_PAYMENT_EXECUTE,
    PERM_PAYMENT_RUN_APPROVE,
    PERM_PAYMENT_VOID,
    PERM_USER_MANAGE,
    PERM_VENDOR_BANK_CHANGE_APPROVE,
    PERM_VENDOR_BLOCK,
    effective_permissions,
)
from app.models.user import User
from app.models.workflow import AuditLog

# The roles whose holders are subject to a periodic access review. These are the
# "elevated" roles — they carry mutate / approve / payment / admin authority.
# ``ap_clerk`` is deliberately excluded: it is the baseline operator role, not a
# privileged grant, so reviewing it as "unused elevated access" is noise.
ELEVATED_ROLES = frozenset({ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO})

# Granular permissions that make a user "elevated" for access-review purposes
# EVEN WHEN none of their roles is one of the three ELEVATED_ROLES names. A
# custom role (`roles.permissions`, see `app/api/permissions.py`) can grant a
# fraud-sensitive permission — most importantly `payment.execute` — to a role
# named anything at all, and a review keyed only on role NAME would never see
# it: exactly the access a SOX access review exists to catch. This list is
# deliberately the money-movement / access-control subset of the full
# permission catalog (not every catalog entry) — the same subset the finding
# that added this called out as fraud-sensitive.
ELEVATED_PERMISSIONS = frozenset(
    {
        PERM_PAYMENT_EXECUTE,
        PERM_PAYMENT_VOID,
        PERM_PAYMENT_RUN_APPROVE,
        PERM_VENDOR_BANK_CHANGE_APPROVE,
        PERM_VENDOR_BLOCK,
        PERM_USER_MANAGE,
    }
)

# Audit-action suffixes that represent *reads*, not mutations. A read does not
# exercise a user's elevated write permission, so it must not reset the dormancy
# clock. (`*.viewed` is written by `log_access`; `audit.exported` by the auditor
# export.) Matched case-insensitively against the action suffix.
_READ_ACTION_SUFFIXES = (".viewed", ".exported")


@dataclass(frozen=True)
class AccessReviewRow:
    """One reviewed user's computed access-review status."""

    user_id: uuid.UUID
    full_name: str
    email: str
    roles: list[str]
    last_privileged_action_at: datetime | None
    dormant: bool
    days_since: int | None  # None when never acted


async def compute_access_review(
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    dormant_after_days: int,
    now: datetime | None = None,
) -> list[AccessReviewRow]:
    """Build the access-review list for ``organization_id``.

    ``control_db`` resolves the users + their roles (control plane); ``tenant_db``
    resolves each actor's last *mutating* audit row (tenant DB). A user is flagged
    DORMANT when their last privileged action is older than ``dormant_after_days``
    or they have never produced a mutating audit row.

    Rows are returned sorted dormant-first, then oldest-activity-first, so the
    review surface leads with the access most in need of revocation.
    """
    now = now or datetime.now(UTC)

    # 1) Elevated users in this org (control plane). selectinload avoids an N+1
    #    on `.roles`.
    users = (
        (
            await control_db.execute(
                select(User)
                .where(User.organization_id == organization_id)
                .where(User.is_active.is_(True))
                .options(selectinload(User.roles))
            )
        )
        .scalars()
        .all()
    )
    elevated: list[tuple[User, list[str]]] = []
    for u in users:
        held = sorted(r.name for r in u.roles)
        has_elevated_role = any(r in ELEVATED_ROLES for r in held)
        has_elevated_permission = bool(effective_permissions(u.roles) & ELEVATED_PERMISSIONS)
        if has_elevated_role or has_elevated_permission:
            elevated.append((u, held))

    if not elevated:
        return []

    # 2) Last *mutating* action per actor (tenant DB). We pull MAX(created_at)
    #    grouped by actor, excluding read-verb actions. The NOT LIKE filters keep
    #    the aggregation in SQL rather than streaming every row into Python.
    actor_ids = [u.id for u, _ in elevated]
    last_action_q = (
        select(AuditLog.actor_id, func.max(AuditLog.created_at))
        .where(AuditLog.organization_id == organization_id)
        .where(AuditLog.actor_id.in_(actor_ids))
        .group_by(AuditLog.actor_id)
    )
    # Exclude read verbs at the SQL layer (case-insensitive suffix match).
    for suffix in _READ_ACTION_SUFFIXES:
        last_action_q = last_action_q.where(~func.lower(AuditLog.action).like(f"%{suffix}"))

    last_by_actor: dict[uuid.UUID, datetime] = {
        actor_id: ts
        for actor_id, ts in (await tenant_db.execute(last_action_q)).all()
        if actor_id is not None
    }

    rows: list[AccessReviewRow] = []
    for u, held in elevated:
        last = last_by_actor.get(u.id)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last is None:
            days_since: int | None = None
            dormant = True
        else:
            days_since = (now - last).days
            dormant = days_since >= dormant_after_days
        rows.append(
            AccessReviewRow(
                user_id=u.id,
                full_name=u.full_name,
                email=u.email,
                roles=held,
                last_privileged_action_at=last,
                dormant=dormant,
                days_since=days_since,
            )
        )

    # Dormant first; within each group, never-acted first, then oldest activity.
    def _sort_key(r: AccessReviewRow) -> tuple:
        never = r.last_privileged_action_at is None
        # datetime.max for "acted" so never-acted (sorted by 0) lands first within
        # the dormant block; among those who acted, oldest timestamp first.
        ts = r.last_privileged_action_at or datetime.min.replace(tzinfo=UTC)
        return (not r.dormant, not never, ts)

    rows.sort(key=_sort_key)
    return rows
