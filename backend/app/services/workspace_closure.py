"""Self-service workspace closure — an administrator deletes an UNUSED workspace.

Why it exists (`docs/decisions.md` §294): `users.email` is globally unique and
one address is one account in one workspace (§291). Someone who signs up with
an address, abandons the trial and is later added to another workspace with
the same address is refused `email_in_other_workspace`, and before this the
only way out was support, because deleting a tenant was operator-only
(`scripts/delete_tenant.py`). Closing the abandoned workspace releases the
address: the deletion removes its user rows, so `identity_provisioning.
email_holder_org_id` no longer finds the address anywhere.

The deletion itself is `services/tenant_deletion.delete_tenant` — documents,
then the tenant database, then the control-plane rows. What this module adds is
what makes it safe to hand to a customer:

**It refuses, fail-closed, anything that is not an unused workspace.** Each
condition in `BLOCKERS` is a reason a closure must go through support instead:
records with a retention obligation or an outside effect (`TENANT_CHECKS`),
other people's accounts (another active user, a supplier-portal login), a
commercial relationship (a paid or provider-backed subscription, a partner
parent or child), and operator-side state (a registered custom domain). A check
that cannot be evaluated counts as a blocker (`records_unverifiable`), never as
a pass.

**Nothing it checked can change before the delete.** Two locks, held from the
checks to the end:

* the organisation row, `FOR UPDATE`, on the control plane. Every insert that
  references the organisation (a user, a subscription) takes `FOR KEY SHARE` on
  that row for its foreign key, and every `settings` writer takes `FOR UPDATE`
  (`tenant.lock_organization`), so none can land while the closure runs. The
  organisation's existing `subscriptions` and `users` rows are locked too,
  because an UPDATE of one (a plan change, a reactivation) does not touch the
  organisation row. The control-plane rows are deleted, and the closure
  recorded complete, in that same transaction
  (`delete_tenant(control_session=…)`).
* `SHARE` on every guarded tenant table, in a transaction kept open until
  `DROP DATABASE … WITH (FORCE)` terminates it. No approval or payment can land
  between the check and the drop.

Two closures of one workspace serialize on a transaction-scoped advisory lock.

**It leaves a record that outlives the tenant.** The tenant's own audit log is
destroyed with its database, so: (1) an `organization.closing` row is written
there and, where audit shipping is on, the tenant's whole unshipped trail is
flushed to the write-once archive BEFORE anything is deleted — a failure
refuses the closure; (2) a control-plane `workspace_closures` row records who
asked, with which proof, and what was removed. That row is also the operator's
queue for the per-tenant backups, which the API cannot reach.

**A partial failure is resumable.** Every step of `delete_tenant` is
idempotent. If the tenant database is already gone and a `started` closure row
exists, the guards passed when that row was written, so a retry goes straight
to finishing the deletion instead of refusing on a database it cannot read.

Logs carry ids and counts only, never an address or the company name.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, exists, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _make_tenant_url, control_session_factory, forget_tenant_engine
from app.models.organization import Organization
from app.models.workspace_closure import (
    CLOSURE_COMPLETED,
    CLOSURE_STARTED,
    WorkspaceClosure,
)
from app.services import audit_log_shipper, tenant_deletion
from app.services.audit_dispatch import record_auth_audit_or_raise

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blockers. Each code is stable: the API returns them in `params.blockers` and
# the SPA states each one (`frontend/src/lib/types/workspaceClosure.ts`).
# ---------------------------------------------------------------------------

# Control plane.
BLOCKER_OTHER_USERS = "other_users"
BLOCKER_PAID_SUBSCRIPTION = "paid_subscription"
BLOCKER_PARTNER_CHILDREN = "partner_children"
BLOCKER_PARTNER_MANAGED = "partner_managed"
BLOCKER_CUSTOM_DOMAIN = "custom_domain"
# Tenant database.
BLOCKER_APPROVED_INVOICES = "approved_invoices"
BLOCKER_PAYMENTS = "payments"
BLOCKER_PAYMENT_RUNS = "payment_runs"
BLOCKER_VIRTUAL_CARDS = "virtual_cards"
BLOCKER_APPLIED_CREDIT_MEMOS = "applied_credit_memos"
BLOCKER_APPROVED_EXPENSE_REPORTS = "approved_expense_reports"
BLOCKER_CARD_TRANSACTIONS = "card_transactions"
BLOCKER_BANK_STATEMENTS = "bank_statements"
BLOCKER_TAX_FILINGS = "tax_filings"
BLOCKER_TAX_RECORDS = "tax_records"
BLOCKER_POSITIVE_PAY_FILES = "positive_pay_files"
BLOCKER_EINVOICE_TRANSMISSIONS = "einvoice_transmissions"
BLOCKER_GOODS_RECEIPTS = "goods_receipts"
BLOCKER_INVENTORY_PUSHES = "inventory_pushes"
BLOCKER_ACTIVE_CONTRACTS = "active_contracts"
BLOCKER_ACCEPTED_DISCOUNTS = "accepted_discounts"
BLOCKER_APPROVED_SUPPLIER_CHANGES = "approved_supplier_changes"
BLOCKER_PRIVACY_REQUESTS = "privacy_requests"
BLOCKER_SUPPLIER_PORTAL_USERS = "supplier_portal_users"
#: A check raised (a missing table, a lock that did not come within the
#: timeout, a dropped connection). Not knowing is not a pass.
BLOCKER_RECORDS_UNVERIFIABLE = "records_unverifiable"

#: Invoice statuses that only exist once someone approved the invoice. Paired
#: with `approved_by IS NOT NULL` in the check, which also catches an approved
#: invoice whose ERP send failed (`failed` is shared with extraction failures,
#: so the status alone cannot say).
_POST_APPROVAL_STATUSES = (
    "approved",
    "sending_to_erp",
    "sent_to_erp",
    "posted_in_erp",
    "payment_scheduled",
    "paid",
    "done",
)


@dataclass(frozen=True)
class TenantCheck:
    code: str
    table: str
    sql: str


def _any_row(table: str, where: str | None = None) -> str:
    clause = f" WHERE {where}" if where else ""
    return f"SELECT EXISTS (SELECT 1 FROM {table}{clause})"


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


#: Fixed SQL only — no value from a request is ever interpolated. The table
#: names are also what the closure locks, so a check cannot be added without
#: its table being held for the rest of the closure.
#:
#: Deliberately NOT here (a trial plausibly holds them and nothing is lost that
#: exists nowhere else): vendors, GL accounts and purchase orders (typically
#: pulled from an ERP, which remains the record), draft or rejected work of
#: every kind, exceptions, sanctions-screening results (evidence only once a
#: payment exists, which blocks on its own), and the audit log itself (shipped
#: to the archive first where shipping is on — decisions §294).
TENANT_CHECKS: tuple[TenantCheck, ...] = (
    TenantCheck(
        BLOCKER_APPROVED_INVOICES,
        "invoices",
        _any_row(
            "invoices",
            f"approved_by IS NOT NULL OR status IN ({_quoted(_POST_APPROVAL_STATUSES)})",
        ),
    ),
    TenantCheck(BLOCKER_PAYMENTS, "payments", _any_row("payments")),
    TenantCheck(BLOCKER_PAYMENT_RUNS, "payment_runs", _any_row("payment_runs")),
    TenantCheck(BLOCKER_VIRTUAL_CARDS, "virtual_cards", _any_row("virtual_cards")),
    TenantCheck(
        BLOCKER_APPLIED_CREDIT_MEMOS,
        "credit_memos",
        _any_row("credit_memos", "status = 'applied' OR applied_at IS NOT NULL"),
    ),
    TenantCheck(
        BLOCKER_APPROVED_EXPENSE_REPORTS,
        "expense_reports",
        _any_row("expense_reports", "status IN ('approved', 'reimbursed')"),
    ),
    TenantCheck(
        BLOCKER_CARD_TRANSACTIONS,
        "corporate_card_transactions",
        _any_row("corporate_card_transactions"),
    ),
    TenantCheck(BLOCKER_BANK_STATEMENTS, "bank_statements", _any_row("bank_statements")),
    TenantCheck(BLOCKER_TAX_FILINGS, "tax_1099_filings", _any_row("tax_1099_filings")),
    TenantCheck(BLOCKER_TAX_RECORDS, "intl_tax_records", _any_row("intl_tax_records")),
    TenantCheck(BLOCKER_POSITIVE_PAY_FILES, "positive_pay_files", _any_row("positive_pay_files")),
    TenantCheck(
        BLOCKER_EINVOICE_TRANSMISSIONS, "peppol_transmissions", _any_row("peppol_transmissions")
    ),
    TenantCheck(BLOCKER_GOODS_RECEIPTS, "goods_receipts", _any_row("goods_receipts")),
    TenantCheck(
        BLOCKER_INVENTORY_PUSHES, "commerce_inventory_syncs", _any_row("commerce_inventory_syncs")
    ),
    TenantCheck(
        BLOCKER_ACTIVE_CONTRACTS,
        "contracts",
        _any_row("contracts", "status IN ('active', 'expired', 'terminated')"),
    ),
    TenantCheck(
        BLOCKER_ACCEPTED_DISCOUNTS,
        "discount_offers",
        _any_row("discount_offers", "status IN ('accepted', 'captured')"),
    ),
    TenantCheck(
        BLOCKER_APPROVED_SUPPLIER_CHANGES,
        "vendor_change_requests",
        _any_row("vendor_change_requests", "status = 'approved'"),
    ),
    TenantCheck(
        BLOCKER_PRIVACY_REQUESTS, "data_subject_requests", _any_row("data_subject_requests")
    ),
    TenantCheck(BLOCKER_SUPPLIER_PORTAL_USERS, "vendor_users", _any_row("vendor_users")),
)

CONTROL_BLOCKERS: tuple[str, ...] = (
    BLOCKER_OTHER_USERS,
    BLOCKER_PAID_SUBSCRIPTION,
    BLOCKER_PARTNER_CHILDREN,
    BLOCKER_PARTNER_MANAGED,
    BLOCKER_CUSTOM_DOMAIN,
)

#: Every code the API can return, in the order a page lists them.
BLOCKERS: tuple[str, ...] = (
    *CONTROL_BLOCKERS,
    *(c.code for c in TENANT_CHECKS),
    BLOCKER_RECORDS_UNVERIFIABLE,
)

#: How long the closure waits for a lock — the organisation row or a guarded
#: table — before giving up (as `records_unverifiable`). A write in flight on an
#: unused workspace is rare; one that holds a lock this long means it is in use.
LOCK_TIMEOUT = "5s"


# ---------------------------------------------------------------------------
# Errors the router maps to coded refusals.
# ---------------------------------------------------------------------------


class ClosureRefused(Exception):
    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__(", ".join(blockers))


class ClosureInProgress(Exception):
    """Another closure of this organisation holds the lock, or finished it."""


class ClosureAuditUnavailable(Exception):
    """The closing audit row could not be written or shipped; nothing was deleted."""


class ClosureIncomplete(Exception):
    """The deletion began and failed part-way; a retry finishes it."""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


async def control_blockers(
    session: AsyncSession, org: Organization, actor_id: uuid.UUID
) -> list[str]:
    """The control-plane reasons this org cannot be closed self-service."""
    found: list[str] = []
    params = {"org": org.id, "actor": actor_id}

    other_users = (
        await session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM users WHERE organization_id = :org "
                "AND is_active AND id <> :actor)"
            ),
            params,
        )
    ).scalar()
    if other_users:
        found.append(BLOCKER_OTHER_USERS)

    # Paid = a live (not canceled) subscription that either the billing
    # provider knows about (it would keep charging an org that no longer
    # exists) or that is active / past due on a plan with a price.
    paid = (
        await session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM subscriptions s JOIN plans p ON p.id = s.plan_id "
                "WHERE s.organization_id = :org AND s.status <> 'canceled' AND ("
                "s.external_subscription_id IS NOT NULL OR "
                "(s.status IN ('active', 'past_due') AND p.monthly_price > 0)))"
            ),
            params,
        )
    ).scalar()
    if paid:
        found.append(BLOCKER_PAID_SUBSCRIPTION)

    children = (
        await session.execute(
            text("SELECT EXISTS (SELECT 1 FROM organizations WHERE parent_org_id = :org)"),
            params,
        )
    ).scalar()
    if children:
        found.append(BLOCKER_PARTNER_CHILDREN)
    if org.parent_org_id is not None:
        found.append(BLOCKER_PARTNER_MANAGED)

    brand = (org.settings or {}).get("brand") if isinstance(org.settings, dict) else None
    domains = brand.get("custom_domains") if isinstance(brand, dict) else None
    if domains:
        found.append(BLOCKER_CUSTOM_DOMAIN)
    return found


async def tenant_blockers(conn: AsyncConnection | AsyncSession) -> list[str]:
    """The tenant-database reasons. Any failure is `records_unverifiable`."""
    found: list[str] = []
    try:
        for check in TENANT_CHECKS:
            if (await conn.execute(text(check.sql))).scalar():
                found.append(check.code)
    except Exception as exc:  # noqa: BLE001 — not knowing is a refusal, never a pass
        logger.warning(
            "[workspace-close] a tenant check could not be evaluated: %s",
            exc.__class__.__name__,
        )
        found.append(BLOCKER_RECORDS_UNVERIFIABLE)
    return found


async def preflight(
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    org: Organization,
    actor_id: uuid.UUID,
) -> list[str]:
    """Every blocker, unlocked — what the page shows before anything is asked."""
    found = await control_blockers(control_db, org, actor_id)
    found += await tenant_blockers(tenant_db)
    if BLOCKER_RECORDS_UNVERIFIABLE in found:
        # The failed statement aborted the request's tenant transaction; leave
        # the session usable for the dependency's teardown.
        await tenant_db.rollback()
    return found


# ---------------------------------------------------------------------------
# The closure
# ---------------------------------------------------------------------------


@dataclass
class ClosureOutcome:
    organization_id: uuid.UUID
    slug: str
    documents_removed: int
    database_dropped: bool
    control_rows_removed: int


def _advisory_key(org_id: uuid.UUID) -> str:
    return f"workspace_close:{org_id}"


async def _database_exists(db_name: str) -> bool:
    async with control_session_factory() as s:
        return bool(
            (
                await s.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :db"), {"db": db_name}
                )
            ).scalar()
        )


async def _started_closure_exists(session: AsyncSession, org_id: uuid.UUID) -> bool:
    return (
        await session.execute(
            select(WorkspaceClosure.id).where(
                WorkspaceClosure.organization_id == org_id,
                WorkspaceClosure.status == CLOSURE_STARTED,
            )
        )
    ).first() is not None


async def _record_started(org_id: uuid.UUID, slug: str, actor_id: uuid.UUID, proof: str) -> None:
    """Committed on its own, BEFORE the deletion: if the run dies part-way, the
    row is what lets a retry resume and what an operator sees."""
    async with control_session_factory() as s:
        stmt = insert(WorkspaceClosure).values(
            id=uuid.uuid4(),
            organization_id=org_id,
            slug=slug,
            requested_by_user_id=actor_id,
            step_up_proof=proof,
            status=CLOSURE_STARTED,
        )
        # A retry after a partial failure re-records who resumed it.
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkspaceClosure.organization_id],
            set_={
                "requested_by_user_id": actor_id,
                "step_up_proof": proof,
                "status": CLOSURE_STARTED,
            },
        )
        await s.execute(stmt)
        await s.commit()


async def _record_completed(
    session: AsyncSession, org_id: uuid.UUID, result: tenant_deletion.TenantDeletionResult
) -> None:
    """On the closure's own transaction, so it commits with the control-plane
    delete: an organisation is gone exactly when its closure reads complete."""
    await session.execute(
        update(WorkspaceClosure)
        .where(WorkspaceClosure.organization_id == org_id)
        .values(
            status=CLOSURE_COMPLETED,
            completed_at=datetime.now(UTC),
            documents_removed=result.objects_deleted,
            database_dropped=result.database_dropped,
            control_rows_removed=sum(result.control_rows_deleted.values()),
        )
    )


async def _write_closing_audit(
    org_id: uuid.UUID, db_name: str, actor_id: uuid.UUID, proof: str
) -> None:
    """The tenant-side record, shipped to the archive before the tenant goes."""
    try:
        await record_auth_audit_or_raise(
            organization_id=org_id,
            actor_id=actor_id,
            action="organization.closing",
            entity_type="organization",
            entity_id=org_id,
            details={"step_up_proof": proof},
        )
        if settings.audit_shipping_enabled:
            await audit_log_shipper.flush_tenant(db_name)
    except Exception as exc:
        logger.warning(
            "[workspace-close] org %s: closing audit not recorded (%s); nothing deleted",
            org_id,
            exc.__class__.__name__,
        )
        raise ClosureAuditUnavailable from exc


async def close_workspace(
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    step_up_proof: str,
) -> ClosureOutcome:
    """Close one workspace: lock, guard, record, delete. See the module docstring.

    Raises `ClosureRefused`, `ClosureInProgress`, `ClosureAuditUnavailable` or
    `ClosureIncomplete`; on success the organisation, its users and its
    database no longer exist, and its closure row reads `completed`.
    """
    async with control_session_factory() as session:
        try:
            outcome, db_name = await _close_locked(
                session, org_id=org_id, actor_id=actor_id, step_up_proof=step_up_proof
            )
            await session.commit()
        except BaseException:
            await session.rollback()
            raise

    # After the commit, and never a reason to report failure: the workspace is
    # gone. A stale pooled engine for a dropped database can only fail later.
    try:
        await forget_tenant_engine(db_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[workspace-close] org %s: cached engine not disposed (%s)",
            org_id,
            exc.__class__.__name__,
        )
    logger.info(
        "[workspace-close] org %s closed by user %s: %d document(s), database %s, "
        "%d control-plane row(s)",
        org_id,
        actor_id,
        outcome.documents_removed,
        "dropped" if outcome.database_dropped else "already absent",
        outcome.control_rows_removed,
    )
    return outcome


async def _close_locked(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, step_up_proof: str
) -> tuple[ClosureOutcome, str]:
    """Everything under the organisation-row lock; returns the outcome and the
    dropped database's name. The caller commits."""
    await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    locked = (
        await session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": _advisory_key(org_id)},
        )
    ).scalar()
    if not locked:
        raise ClosureInProgress

    try:
        org = (
            await session.execute(
                select(Organization)
                .where(Organization.id == org_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        # The org-row lock holds off every INSERT that references it (its
        # foreign key takes KEY SHARE on that row) and every settings writer,
        # but not an UPDATE of a row that already exists: a billing webhook
        # moving the free subscription onto a priced plan, or a deactivated
        # user being reactivated. Lock those rows too, so what the blockers
        # read below is what the deletion removes.
        for table in ("subscriptions", "users"):
            await session.execute(
                text(f"SELECT 1 FROM {table} WHERE organization_id = :org FOR UPDATE"),
                {"org": org_id},
            )
    except Exception as exc:  # noqa: BLE001 — a lock we cannot take is a refusal
        logger.warning(
            "[workspace-close] org %s: organisation row not lockable (%s)",
            org_id,
            exc.__class__.__name__,
        )
        raise ClosureRefused([BLOCKER_RECORDS_UNVERIFIABLE]) from exc
    if org is None:
        # Gone between the request's own lookup and this lock: another
        # closure (or an operator) finished it.
        raise ClosureInProgress
    slug, db_name = org.slug, org.db_name

    blockers = await control_blockers(session, org, actor_id)
    if blockers:
        raise ClosureRefused(blockers)

    resuming = not await _database_exists(db_name) and await _started_closure_exists(
        session, org_id
    )
    if resuming:
        logger.info("[workspace-close] org %s: resuming a closure whose database is gone", org_id)
        result = await _delete(slug, session)
    else:
        result = await _guard_and_delete(
            session,
            org_id=org_id,
            slug=slug,
            db_name=db_name,
            actor_id=actor_id,
            step_up_proof=step_up_proof,
        )

    await _record_completed(session, org_id, result)
    outcome = ClosureOutcome(
        organization_id=org_id,
        slug=slug,
        documents_removed=result.objects_deleted,
        database_dropped=result.database_dropped,
        control_rows_removed=sum(result.control_rows_deleted.values()),
    )
    return outcome, db_name


async def _guard_and_delete(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    slug: str,
    db_name: str,
    actor_id: uuid.UUID,
    step_up_proof: str,
) -> tenant_deletion.TenantDeletionResult:
    """Re-check under locks held until the database drop, then delete."""
    engine = create_async_engine(_make_tenant_url(db_name), poolclass=NullPool)
    conn: AsyncConnection | None = None
    try:
        try:
            conn = await engine.connect()
            await conn.begin()
            await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            tables = ", ".join(check.table for check in TENANT_CHECKS)
            await conn.execute(text(f"LOCK TABLE {tables} IN SHARE MODE"))
        except Exception as exc:  # noqa: BLE001 — a lock we cannot take is a refusal
            logger.warning(
                "[workspace-close] org %s: guarded tables not lockable (%s)",
                org_id,
                exc.__class__.__name__,
            )
            raise ClosureRefused([BLOCKER_RECORDS_UNVERIFIABLE]) from exc
        blockers = await tenant_blockers(conn)
        if blockers:
            raise ClosureRefused(blockers)

        # Recorded before anything is destroyed, and refused if it cannot be.
        await _write_closing_audit(org_id, db_name, actor_id, step_up_proof)
        await _record_started(org_id, slug, actor_id, step_up_proof)

        # The locks are still held here. `DROP DATABASE … WITH (FORCE)` inside
        # the deletion terminates this very connection, which is what releases
        # them — there is no moment between the check and the drop when a
        # guarded row could be written.
        return await _delete(slug, session)
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 — terminated by the drop, as intended
                pass
        await engine.dispose()


#: The lock wait allowed once the deletion has begun. Every row the closure
#: checked is already locked by now, so a wait here is on something unrelated;
#: giving up after the database is gone only costs a retry (the resume path),
#: but a longer wait is cheaper than that.
DELETE_LOCK_TIMEOUT = "30s"


async def _delete(slug: str, session: AsyncSession) -> tenant_deletion.TenantDeletionResult:
    """`delete_tenant`, with the control-plane rows deleted on ``session`` — the
    transaction holding the organisation row's lock."""
    await session.execute(text(f"SET LOCAL lock_timeout = '{DELETE_LOCK_TIMEOUT}'"))
    try:
        return await tenant_deletion.delete_tenant(slug, control_session=session)
    except Exception as exc:
        logger.error(
            "[workspace-close] deletion of a closed workspace failed part-way (%s); "
            "a retry finishes it",
            exc.__class__.__name__,
        )
        raise ClosureIncomplete from exc


# ---------------------------------------------------------------------------
# The operator's half: backups (scripts/workspace_closures.py)
# ---------------------------------------------------------------------------


async def closures_owing_backups() -> list[WorkspaceClosure]:
    """Closures whose backup dumps an operator has not yet removed.

    `/legal/dpa` § 13 commits to deleting a tenant's backup objects within 60
    days; the API cannot reach the backup bucket, so each row here is owed a
    `deploy/remove-tenant.sh <slug>` run (its backup leg works on a tenant that
    is already gone). Oldest first — the oldest is closest to that deadline.

    A `started` row whose organisation no longer exists is listed too: the
    completion is written in the same transaction as the control-plane delete,
    so that should not happen, and if it ever does the backups are still owed.
    """
    org_gone = ~exists().where(Organization.id == WorkspaceClosure.organization_id)
    async with control_session_factory() as s:
        return list(
            (
                await s.execute(
                    select(WorkspaceClosure)
                    .where(
                        WorkspaceClosure.backups_cleared_at.is_(None),
                        or_(
                            WorkspaceClosure.status == CLOSURE_COMPLETED,
                            and_(WorkspaceClosure.status == CLOSURE_STARTED, org_gone),
                        ),
                    )
                    .order_by(WorkspaceClosure.started_at.asc())
                )
            )
            .scalars()
            .all()
        )


async def mark_backups_cleared(organization_id: uuid.UUID) -> int:
    """Record that a closed workspace's backups are gone. Returns rows marked.

    Keyed by organisation id, not slug: a slug becomes reusable once its backups
    are cleared (`utils/slug.ensure_slug_available`), and the id is the one
    thing two workspaces that shared a name never share. Only a closure whose
    organisation is gone is marked — a closure still running has backups nobody
    removed.
    """
    org_gone = ~exists().where(Organization.id == WorkspaceClosure.organization_id)
    async with control_session_factory() as s:
        result = await s.execute(
            update(WorkspaceClosure)
            .where(
                WorkspaceClosure.organization_id == organization_id,
                WorkspaceClosure.backups_cleared_at.is_(None),
                org_gone,
            )
            .values(backups_cleared_at=datetime.now(UTC))
        )
        await s.commit()
        return result.rowcount or 0
