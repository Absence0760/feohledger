"""Tenant deletion — the inverse of ``tenant_provisioning``.

Shared by ``scripts/delete_tenant.py`` (which ``deploy/remove-tenant.sh``
drives). This is the code behind a published promise: ``/legal/dpa`` § 13 says
deletion destroys the tenant database, the control-plane organisation and user
records, and the object-storage key prefix holding uploaded documents, within
60 days. Clause 10 of the Terms says the same to the customer signing it.

**The order is load-bearing, and it is not the obvious one.** The database
first, then object storage, then the control-plane rows:

    feoh_<slug>  →  documents  →  organizations / users / …

The instinct is to remove the control-plane row first, because that is what
makes a tenant reachable. Doing so is unrecoverable. Every object key is
``{org_id}/…`` and the organisation row is the only thing that knows that id —
delete it and then fail on the sweep, and there is no longer any way to find
the documents that were supposed to go (decisions §179).

The database goes before the documents (decisions §306, which reversed the
original documents-first order). Both steps are irreversible, but only one
order leaves a coherent state when the other fails: a database that would not
drop after its documents were swept was a LIVE workspace whose invoices pointed
at files that no longer existed, while documents that would not sweep after the
drop belong to a workspace that is already unusable, under a prefix the org row
still names, and a re-run removes them. Dropping first also means no upload can
commit while the sweep runs, and the sweep makes a second pass for an object an
in-flight request PUT after the first listing. Every step is idempotent and a
partial failure is simply re-run: ``DROP DATABASE IF EXISTS`` is a no-op, the
prefix sweep finds nothing, and the control-plane transaction finishes the job.

The transient this trades for is real and accepted: between the database drop
and the control-plane delete, the organisation row points at a database that is
gone, so an API request for that tenant errors. Since docs/decisions.md §364
the tenant's host is CloudFront's wildcard and keeps serving the static SPA,
so nothing in front of the API stops serving it first; the window is the few
seconds between the two steps, on a tenant whose termination is already
under way, and ends with the API answering the slug as an unknown tenant.

**An ERP OAuth grant is revoked on the way out** (decisions §371). Its tokens
are sealed in ``provider_credentials``, which the control-plane step deletes, so
just before that step they are read once and the provider is asked to revoke
the refresh token. Otherwise it stays valid at Intuit / Xero / Sage until it
expires, up to five years for QuickBooks. It comes after the database drop and
the document sweep, so a workspace whose drop failed is never left live with
its ERP cut off. A self-service closure holds row locks on its control-plane
transaction, so there the tokens are read inside it and the provider is called
only once it has committed (``post_commit``): no provider's latency is charged
to a transaction holding locks. Best-effort: a refusal, an outage, a provider
with no revocation endpoint (Blackbaud) or a store that cannot be opened never
stops the deletion. The outcome goes into the written confirmation (the
operator's CLI) or the deletion log (a closure), so the operator can tell the
customer what to remove on the provider's side. A consent parked for
the company picker (decisions §355) is not revoked, for §337's reason, and
lapses with its Redis TTL.

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
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import control_session_factory
from app.services import storage
from app.services.credential_crypto import CredentialCryptoError
from app.services.post_commit import enqueue_post_commit
from app.services.tenant_provisioning import (
    CONTROL_TABLES,
    _assert_safe_db_name,
    _drop_postgres_database,
)

logger = logging.getLogger(__name__)


class TenantDeletionError(RuntimeError):
    """A deletion that must not proceed — unknown slug, or a parent org."""


class TenantNotFound(TenantDeletionError):
    """No organisation has the slug: already deleted, or never existed.

    Its own type because it is the one refusal a caller may treat as "nothing
    left to do here" — ``deploy/remove-tenant.sh`` finishes the backup and Caddy
    legs on it — where every other refusal must stop the run.
    """


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
    # The org's sealed provider credentials. Deleting the row is enough: the
    # ciphertext is useless without its wrapped data key, which goes with it.
    ("provider_credentials", "DELETE FROM provider_credentials WHERE organization_id = :org"),
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
    "workspace_closures": (
        "The record that a tenant was closed by its own administrator "
        "(services/workspace_closure.py, decisions §306). It exists to outlive "
        "the deletion it describes: the tenant's own audit log goes with its "
        "database, so this row is the evidence that the closure was asked for, "
        "by whom and with which proof, and the operator's queue for the backup "
        "leg the API cannot reach. It holds ids and the slug, no personal data."
    ),
    "subprocessor_notices": (
        "The record that this organisation was given the sub-processor change "
        "notice the DPA promises (services/subprocessor_notices.py, decisions "
        "§321). It is evidence of a contractual notice having been given, so it "
        "outlives the customer it was given to. Ids, dates and counts only — no "
        "address or name."
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
    #: The tenant's ERP OAuth grant, read before its sealed tokens went with
    #: the control plane and revoked best-effort (decisions §371): the
    #: provider key and one of ``erp_oauth.REVOKED`` / ``REVOKE_FAILED`` /
    #: ``REVOKE_UNSUPPORTED``, or :data:`GRANT_UNREADABLE`. On a closure's
    #: session it reads :data:`GRANT_REVOKE_AFTER_COMMIT` until that session
    #: commits and the provider has answered, then the outcome. Both None when
    #: the tenant held no grant.
    erp_grant_provider: str | None = None
    erp_grant_revocation: str | None = None


#: The sealed store could not be opened, so the tokens could not be read to
#: revoke them. The deletion still proceeds; the confirmation says so.
GRANT_UNREADABLE = "unreadable"

#: The tokens were read on a closure's transaction, and the provider is told
#: once that transaction commits (its locks released), never while it holds
#: them. The result is updated with the outcome when the provider answers, and
#: the outcome is logged either way. A rolled-back closure revokes nothing: its
#: sealed row survives, and the resumed closure reads it again.
GRANT_REVOKE_AFTER_COMMIT = "after_commit"

_REMOVE_AT_PROVIDER = "the customer should remove FeohLedger from the provider's connected apps"

#: How the written confirmation states each revocation outcome. Anything short
#: of "revoked" says what is still live, because the customer's books stay
#: reachable through the grant until the provider is told or it expires.
_GRANT_OUTCOME_TEXT = {
    "revoked": "revoked at the provider",
    "failed": (
        "NOT revoked (the provider refused or could not be reached); " + _REMOVE_AT_PROVIDER
    ),
    "unsupported": ("NOT revoked: the provider has no revocation endpoint; " + _REMOVE_AT_PROVIDER),
    GRANT_UNREADABLE: (
        "NOT revoked: the sealed tokens could not be opened; " + _REMOVE_AT_PROVIDER
    ),
    GRANT_REVOKE_AFTER_COMMIT: (
        "revocation requested once the closure commits; NOT yet confirmed, the outcome "
        "is in the deletion log; unless it says revoked, " + _REMOVE_AT_PROVIDER
    ),
}


@dataclass
class _ErpGrant:
    """What the deletion read about the tenant's ERP OAuth grant.

    ``outcome`` is set when the read already decided it (unreadable, failed to
    read); otherwise ``spec`` / ``erp_settings`` / ``oauth`` are what
    ``erp_oauth.revoke_grant`` needs, and none of it needs a database."""

    provider: str
    outcome: str | None = None
    spec: Any = None
    erp_settings: dict = field(default_factory=dict)
    oauth: dict = field(default_factory=dict)

    async def revoke(self) -> str:
        from app.services import erp_oauth

        if self.outcome is not None:
            return self.outcome
        return await erp_oauth.revoke_grant(self.spec, self.erp_settings, self.oauth)


def _log_grant_outcome(slug: str, provider: str, outcome: str) -> None:
    """Provider and outcome only: never a token."""
    log = logger.info if outcome == "revoked" else logger.warning
    log("[tenant-deletion] %s: ERP OAuth grant (%s) %s", slug, provider, outcome)


async def _load_plan(session, slug: str) -> TenantDeletionPlan:
    row = (
        await session.execute(
            text("SELECT id, name, db_name FROM organizations WHERE slug = :slug"),
            {"slug": slug},
        )
    ).first()
    if row is None:
        raise TenantNotFound(
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


async def delete_tenant(
    slug: str,
    *,
    dry_run: bool = False,
    control_session: AsyncSession | None = None,
) -> TenantDeletionResult:
    """Delete a tenant completely. See the module docstring for the ordering.

    Idempotent: re-running after a partial failure finishes the job rather than
    erroring, which is why the irreversible steps come before the row that
    identifies them.

    ``control_session``: run step 3 on the caller's open transaction instead of
    a fresh one, and leave the commit to the caller. A self-service closure
    (`services/workspace_closure`) holds ``FOR UPDATE`` on the organisation row
    for its whole run, so that no user, subscription or setting can be added
    between its checks and the delete; deleting that row from any OTHER session
    would wait on its own lock. The ERP grant's provider call is then queued
    for after that commit (``post_commit``), so the returned result reads
    :data:`GRANT_REVOKE_AFTER_COMMIT` until the provider answers.
    """
    async with control_session_factory() as session:
        plan = await _load_plan(session, slug)

    if dry_run:
        return TenantDeletionResult(
            plan=plan, objects_deleted=0, database_dropped=False, dry_run=True
        )

    _assert_safe_db_name(plan.db_name)

    # 1. The tenant database, with every invoice, vendor, payment and audit row
    #    in it. WITH (FORCE) terminates live connections — a session still open
    #    on a tenant being deleted must not be able to veto the deletion. First,
    #    so a drop that fails leaves the workspace whole (see the docstring).
    database_dropped = False
    if plan.database_exists:
        await _drop_postgres_database(plan.db_name)
        database_dropped = True
        logger.info("[tenant-deletion] %s: dropped database %s", slug, plan.db_name)

    # 2. Documents. Must precede the control-plane delete: the org id is the
    #    prefix, and the org row is the only place it is written down. Two
    #    passes: a request that was mid-upload when the database went may have
    #    PUT its object after the first listing; nothing can commit a new one
    #    now that the database is gone, so the second pass is the last word.
    prefix = f"{plan.organization_id}/"
    objects_deleted = await storage.delete_prefix(prefix)
    objects_deleted += await storage.delete_prefix(prefix)
    logger.info(
        "[tenant-deletion] %s: removed %d object(s) under %s", slug, objects_deleted, prefix
    )

    # 2b. The ERP OAuth grant. Its tokens are sealed in `provider_credentials`,
    #     which step 3 deletes: the last moment they can be read to tell the
    #     provider. After steps 1 and 2, so a drop that failed leaves the
    #     workspace whole AND connected. Best-effort: a provider that refuses or
    #     is unreachable must not keep a tenant alive, so the outcome is
    #     reported, never raised. Provider and outcome only in the log.
    #     On a closure's session the provider is called after its commit (see
    #     below), never while the closure holds its row locks.
    grant = await _read_erp_grant(plan, control_session)
    grant_revocation: str | None = None
    if grant is not None:
        if control_session is None:
            grant_revocation = await grant.revoke()
            _log_grant_outcome(slug, grant.provider, grant_revocation)
        else:
            grant_revocation = grant.outcome or GRANT_REVOKE_AFTER_COMMIT
            if grant.outcome is not None:
                _log_grant_outcome(slug, grant.provider, grant.outcome)

    # 3. The control plane, in one transaction: either the tenant is gone from
    #    it or nothing changed, never a half-deleted org.
    if control_session is not None:
        deleted = await _delete_control_rows(control_session, plan)
    else:
        async with control_session_factory() as session:
            async with session.begin():
                deleted = await _delete_control_rows(session, plan)

    logger.info(
        "[tenant-deletion] %s: removed %d control-plane row(s) across %d table(s)",
        slug,
        sum(deleted.values()),
        len(deleted),
    )
    result = TenantDeletionResult(
        plan=plan,
        objects_deleted=objects_deleted,
        database_dropped=database_dropped,
        control_rows_deleted=deleted,
        erp_grant_provider=grant.provider if grant is not None else None,
        erp_grant_revocation=grant_revocation,
    )
    if grant is not None and grant_revocation == GRANT_REVOKE_AFTER_COMMIT:
        _revoke_after_commit(control_session, slug, grant, result)
    return result


def _revoke_after_commit(
    session: AsyncSession, slug: str, grant: _ErpGrant, result: TenantDeletionResult
) -> None:
    """Queue the provider call for after ``session`` commits.

    ``post_commit`` runs it once the closure's transaction has committed, so
    every row lock it held is released first (root ``CLAUDE.md``: no third
    party's latency is charged to a transaction holding locks). A rollback
    drops it: the organisation and its sealed row are still there, and the
    resumed closure reads and revokes again. Best-effort like the rest of the
    step: ``revoke_grant`` never raises, and the outcome lands on ``result``
    and in the log.
    """

    async def _job() -> None:
        outcome = await grant.revoke()
        result.erp_grant_revocation = outcome
        _log_grant_outcome(slug, grant.provider, outcome)

    logger.info(
        "[tenant-deletion] %s: ERP OAuth grant (%s) to be revoked once the closure commits",
        slug,
        grant.provider,
    )
    enqueue_post_commit(session, _job, name="tenant-deletion-erp-revoke")


async def _read_erp_grant(
    plan: TenantDeletionPlan, control_session: AsyncSession | None
) -> _ErpGrant | None:
    """Read the tenant's ERP OAuth grant for revocation, or None with no grant.

    A tenant holds at most one: ``settings.erp.oauth`` is a single block and
    its tokens two fixed sealed paths. Calls no provider.

    Reads on ``control_session`` when given (a closure holding the org row),
    inside a SAVEPOINT, so a failed read (a lock timeout, say) rolls back to it
    and leaves the closure's transaction usable for the control-plane delete.
    Without a ``control_session`` it reads on a short session of its own,
    CLOSED before the caller calls the provider. Never raises: a store that
    cannot be opened is :data:`GRANT_UNREADABLE`, anything else unexpected
    ``failed``, each only when the metadata says a grant existed.
    """
    from app.services import erp_oauth

    named: str | None = None  # the provider the consent metadata names, if any

    async def _read(session: AsyncSession) -> tuple[dict, dict]:
        nonlocal named
        org_settings = (
            await session.execute(
                text("SELECT settings FROM organizations WHERE id = :org"),
                {"org": plan.organization_id},
            )
        ).scalar_one_or_none()
        erp = (org_settings or {}).get("erp") if isinstance(org_settings, dict) else None
        meta = erp.get("oauth") if isinstance(erp, dict) else None
        if isinstance(meta, dict):
            named = str(meta.get("provider") or "") or None
        return await erp_oauth.resolved_erp(plan.organization_id, org_settings, session)

    try:
        if control_session is not None:
            async with control_session.begin_nested():
                resolved, oauth = await _read(control_session)
        else:
            async with control_session_factory() as session:
                resolved, oauth = await _read(session)
    except CredentialCryptoError:
        # The tokens cannot be read, so they cannot be revoked. Reported when
        # the metadata says a grant existed; the store is about to go anyway.
        return _ErpGrant(named, outcome=GRANT_UNREADABLE) if named else None
    except Exception as exc:  # noqa: BLE001 — a deletion is never stopped by its courtesy call
        logger.warning(
            "[tenant-deletion] %s: ERP grant could not be read (%s)",
            plan.slug,
            exc.__class__.__name__,
        )
        return _ErpGrant(named, outcome=erp_oauth.REVOKE_FAILED) if named else None

    if not any(oauth.get(path.split(".", 1)[1]) for path in erp_oauth.TOKEN_PATHS):
        return None
    provider = str(oauth.get("provider") or "") or "unknown"
    spec = erp_oauth.load_providers().get(provider)
    grant = _ErpGrant(provider, spec=spec, erp_settings=resolved, oauth=oauth)
    if spec is None or not erp_oauth.revoke_endpoint(spec):
        # Nothing to call (an unknown provider, or Blackbaud's missing
        # endpoint): decided now, so nothing waits for a commit.
        grant.outcome = await erp_oauth.revoke_grant(spec, resolved, oauth)
    return grant


async def _delete_control_rows(session: AsyncSession, plan: TenantDeletionPlan) -> dict[str, int]:
    """Step 3's statements, on ``session``; the caller owns the transaction."""
    deleted: dict[str, int] = {}
    for table, sql in CONTROL_DELETIONS:
        result = await session.execute(text(sql), {"org": plan.organization_id, "slug": plan.slug})
        deleted[table] = result.rowcount or 0
    return deleted


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
    if result.erp_grant_provider is not None:
        lines.append(
            f"  ERP OAuth grant:      {result.erp_grant_provider} — "
            + _GRANT_OUTCOME_TEXT.get(
                result.erp_grant_revocation or "", _GRANT_OUTCOME_TEXT["failed"]
            )
        )
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
