"""Tenant deletion — the inverse of ``tenant_provisioning``.

Shared by ``scripts/delete_tenant.py`` (which ``deploy/remove-tenant.sh``
drives). This is the code behind a published promise: ``/legal/dpa`` § 13 says
deletion destroys the tenant database, the control-plane organisation and user
records, and the object-storage key prefix holding uploaded documents, within
60 days. Clause 10 of the Terms says the same to the customer signing it.

**The order is load-bearing, and it is not the obvious one.** Object storage
first, then the database, then the control-plane rows:

    documents  →  feoh_<slug>  →  organizations / users / …

The instinct is to remove the control-plane row first, because that is what
makes a tenant reachable. Doing so is unrecoverable. Every object key is
``{org_id}/…`` and the organisation row is the only thing that knows that id —
delete it and then fail on the sweep, and there is no longer any way to find
the documents that were supposed to go. Ordered this way, every step is
idempotent and a partial failure is simply re-run: the prefix sweep finds
nothing, ``DROP DATABASE IF EXISTS`` is a no-op, and the control-plane
transaction finishes the job.

The transient this trades for is real and accepted: between the database drop
and the control-plane delete, the organisation row points at a database that is
gone, so a request on that tenant's host errors. ``deploy/remove-tenant.sh``
removes the Caddy host block and reloads *before* calling this, so in the
deployment path nothing is serving that host by then.

**What this does not reach** is disclosed rather than quietly omitted, because
the DPA discloses it too: backup objects (the deploy script removes the
per-tenant dumps; the shared control-plane dump is not selectively editable and
ages out on the 90-day retention cycle), and any audit event already shipped to
a write-once archive, which cannot be deleted before its retention expires.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from app.database import control_session_factory
from app.services import storage
from app.services.tenant_provisioning import (
    CONTROL_TABLES,
    _assert_safe_db_name,
    _drop_postgres_database,
)

logger = logging.getLogger(__name__)


class TenantDeletionError(RuntimeError):
    """A deletion that must not proceed — unknown slug, or a parent org."""


# ---------------------------------------------------------------------------
# What a deletion sweeps out of the control plane.
#
# Ordered: a row referencing another must go first, because the FKs between
# these tables are plain references with no ON DELETE CASCADE. Postgres would
# otherwise refuse the organisations delete at the end, which is the behaviour
# we want (a silent cascade would let a forgotten table slip out of this list
# unnoticed) — so the order is the check.
#
# `tests/test_tenant_deletion.py` holds this against `CONTROL_TABLES`: every
# control-plane table is either swept here or named in CONTROL_TABLES_EXEMPT
# with a reason, so a NEW control-plane table cannot be added without someone
# deciding which it is. That is the same drift a register or a docstring has,
# and the same answer.
# ---------------------------------------------------------------------------

CONTROL_DELETIONS: tuple[tuple[str, str], ...] = (
    ("webhook_deliveries", "DELETE FROM webhook_deliveries WHERE organization_id = :org"),
    ("webhook_subscriptions", "DELETE FROM webhook_subscriptions WHERE organization_id = :org"),
    ("api_key_usage", "DELETE FROM api_key_usage WHERE organization_id = :org"),
    ("api_keys", "DELETE FROM api_keys WHERE organization_id = :org"),
    # No FK at all on this one (a plain indexed UUID column), so nothing would
    # have complained had it been forgotten — it would just have kept a
    # deleted org's token meter forever.
    ("assistant_usage", "DELETE FROM assistant_usage WHERE organization_id = :org"),
    ("subscriptions", "DELETE FROM subscriptions WHERE organization_id = :org"),
    (
        "webauthn_credentials",
        "DELETE FROM webauthn_credentials WHERE user_id IN "
        "(SELECT id FROM users WHERE organization_id = :org)",
    ),
    (
        "user_roles",
        "DELETE FROM user_roles WHERE user_id IN "
        "(SELECT id FROM users WHERE organization_id = :org)",
    ),
    ("users", "DELETE FROM users WHERE organization_id = :org"),
    # Custom roles only. A system role (admin / ap_manager / ap_clerk / cfo)
    # carries organization_id IS NULL and is shared by every tenant on the
    # platform; `= :org` never matches NULL, so this cannot reach one. That is
    # the whole safety argument, and it is why this is not `IS NOT DISTINCT
    # FROM` or a Python-side filter.
    ("roles", "DELETE FROM roles WHERE organization_id = :org"),
    # Keyed by slug, not by org: a pending signup for this slug was never
    # provisioned, so it has no organisation — but it holds the email and name
    # of the person who started it, which is this customer's personal data.
    ("email_verifications", "DELETE FROM email_verifications WHERE slug = :slug"),
    ("organizations", "DELETE FROM organizations WHERE id = :org"),
)

#: Control-plane tables a tenant deletion deliberately does not touch.
CONTROL_TABLES_EXEMPT: dict[str, str] = {
    "plans": (
        "Sellable billing tiers, not tenant data — one catalogue shared by every "
        "organisation on the platform. Deleting a tenant's plan would delete it "
        "for everyone. The tenant's link to a plan is its `subscriptions` row, "
        "which IS swept."
    ),
}


@dataclass
class TenantDeletionPlan:
    """What a deletion would remove. Produced before anything is destroyed."""

    organization_id: uuid.UUID
    slug: str
    name: str
    db_name: str
    database_exists: bool
    #: Rows per control-plane table, counted before the delete.
    control_rows: dict[str, int] = field(default_factory=dict)

    @property
    def total_control_rows(self) -> int:
        return sum(self.control_rows.values())


@dataclass
class TenantDeletionResult:
    """What a deletion actually removed — the basis of the written confirmation
    ``/legal/dpa`` § 13 promises the customer."""

    plan: TenantDeletionPlan
    objects_deleted: int
    database_dropped: bool
    control_rows_deleted: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False


async def _load_plan(session, slug: str) -> TenantDeletionPlan:
    row = (
        await session.execute(
            text("SELECT id, name, db_name FROM organizations WHERE slug = :slug"),
            {"slug": slug},
        )
    ).first()
    if row is None:
        raise TenantDeletionError(
            f"no organisation with slug {slug!r} — nothing to delete. "
            "(A tenant already deleted leaves no row; this is also what a typo looks like.)"
        )
    org_id, name, db_name = row

    children = (
        (
            await session.execute(
                text("SELECT slug FROM organizations WHERE parent_org_id = :org ORDER BY slug"),
                {"org": org_id},
            )
        )
        .scalars()
        .all()
    )
    if children:
        # `organizations.parent_org_id` is ON DELETE SET NULL, so Postgres would
        # accept this delete and silently promote every child to a standalone
        # tenant — a reseller's customers, detached from the partner that
        # administers them, with nothing in the log saying it happened. Refusing
        # makes the operator decide what should become of them first.
        raise TenantDeletionError(
            f"{slug!r} is a partner organisation with {len(children)} child tenant(s): "
            f"{', '.join(children)}. Deleting it would silently orphan them "
            "(parent_org_id is ON DELETE SET NULL). Re-parent or delete the children first."
        )

    database_exists = bool(
        (
            await session.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db"), {"db": db_name}
            )
        ).scalar()
    )

    plan = TenantDeletionPlan(
        organization_id=org_id,
        slug=slug,
        name=name,
        db_name=db_name,
        database_exists=database_exists,
    )
    for table, _sql in CONTROL_DELETIONS:
        count_sql = _count_sql_for(table)
        plan.control_rows[table] = (
            await session.execute(count_sql, {"org": org_id, "slug": slug})
        ).scalar_one()
    return plan


def _count_sql_for(table: str):
    """The SELECT COUNT matching a table's DELETE, derived from it.

    Derived rather than written twice: a count whose WHERE clause drifted from
    the delete's would report a number the deletion does not act on, which is
    worse than no report at all — the operator's written confirmation to the
    customer is built from it.
    """
    for name, sql in CONTROL_DELETIONS:
        if name == table:
            return text(sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1))
    raise KeyError(table)


async def plan_tenant_deletion(slug: str) -> TenantDeletionPlan:
    """Survey what deleting ``slug`` would remove. Destroys nothing."""
    async with control_session_factory() as session:
        return await _load_plan(session, slug)


async def delete_tenant(slug: str, *, dry_run: bool = False) -> TenantDeletionResult:
    """Delete a tenant completely. See the module docstring for the ordering.

    Idempotent: re-running after a partial failure finishes the job rather than
    erroring, which is why the irreversible steps come before the row that
    identifies them.
    """
    async with control_session_factory() as session:
        plan = await _load_plan(session, slug)

    if dry_run:
        return TenantDeletionResult(
            plan=plan, objects_deleted=0, database_dropped=False, dry_run=True
        )

    _assert_safe_db_name(plan.db_name)

    # 1. Documents. Must precede the control-plane delete: the org id is the
    #    prefix, and the org row is the only place it is written down.
    prefix = f"{plan.organization_id}/"
    objects_deleted = await storage.delete_prefix(prefix)
    logger.info(
        "[tenant-deletion] %s: removed %d object(s) under %s", slug, objects_deleted, prefix
    )

    # 2. The tenant database, with every invoice, vendor, payment and audit row
    #    in it. WITH (FORCE) terminates live connections — a session still open
    #    on a tenant being deleted must not be able to veto the deletion.
    database_dropped = False
    if plan.database_exists:
        await _drop_postgres_database(plan.db_name)
        database_dropped = True
        logger.info("[tenant-deletion] %s: dropped database %s", slug, plan.db_name)

    # 3. The control plane, in one transaction: either the tenant is gone from
    #    it or nothing changed, never a half-deleted org.
    deleted: dict[str, int] = {}
    async with control_session_factory() as session:
        async with session.begin():
            for table, sql in CONTROL_DELETIONS:
                result = await session.execute(
                    text(sql), {"org": plan.organization_id, "slug": slug}
                )
                deleted[table] = result.rowcount or 0

    logger.info(
        "[tenant-deletion] %s: removed %d control-plane row(s) across %d table(s)",
        slug,
        sum(deleted.values()),
        len(deleted),
    )
    return TenantDeletionResult(
        plan=plan,
        objects_deleted=objects_deleted,
        database_dropped=database_dropped,
        control_rows_deleted=deleted,
    )


def format_confirmation(result: TenantDeletionResult) -> str:
    """The written record of what was destroyed.

    ``/legal/dpa`` § 13 commits to confirming deletion in writing. This is the
    text to send: specific about what went, and specific about the two residues
    that outlive it, because a confirmation that overstates is worse than none.
    """
    plan = result.plan
    lines = [
        f"Tenant deletion — {plan.name} ({plan.slug})",
        f"  organisation id:      {plan.organization_id}",
        f"  documents removed:    {result.objects_deleted} object(s) under {plan.organization_id}/",
        f"  tenant database:      {plan.db_name} "
        + ("dropped" if result.database_dropped else "was already absent"),
        f"  control-plane rows:   {sum(result.control_rows_deleted.values())} across "
        f"{len(result.control_rows_deleted)} table(s)",
    ]
    for table, count in result.control_rows_deleted.items():
        if count:
            lines.append(f"      {table}: {count}")
    lines += [
        "",
        "  Not reached by this step, as disclosed in /legal/dpa § 13:",
        "    - per-tenant backup dumps (deploy/remove-tenant.sh removes these);",
        "    - the shared control-plane dump, which is not selectively editable",
        "      and ages out on the ordinary backup retention cycle;",
        "    - audit events already shipped to write-once archival, which cannot",
        "      be deleted before their retention period expires.",
    ]
    return "\n".join(lines)


# Deliberately evaluated at import: a mismatch between the sweep and the
# control-plane table set is a completeness bug in a deletion, and the earliest
# honest place to find it is before anything runs. The test of the same name
# carries the explanation.
_swept = {table for table, _sql in CONTROL_DELETIONS}
_unaccounted = CONTROL_TABLES - _swept - set(CONTROL_TABLES_EXEMPT)
if _unaccounted:  # pragma: no cover - guarded by tests/test_tenant_deletion.py
    raise RuntimeError(
        "control-plane table(s) neither swept by a tenant deletion nor exempted: "
        + ", ".join(sorted(_unaccounted))
        + " — add a DELETE to CONTROL_DELETIONS or an entry to CONTROL_TABLES_EXEMPT."
    )
