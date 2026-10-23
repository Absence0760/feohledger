"""Self-service workspace closure — an administrator deletes an UNUSED workspace.

Why it exists (`docs/decisions.md` §306): `users.email` is globally unique and
one address is one account in one workspace (§291). Someone who signs up with
an address, abandons the trial and is later added to another workspace with
the same address is refused `email_in_other_workspace`, and before this the
only way out was support, because deleting a tenant was operator-only
(`scripts/delete_tenant.py`). Closing the abandoned workspace releases the
address: the deletion removes its user rows, so `identity_provisioning.
email_holder_org_id` no longer finds the address anywhere.

The deletion itself is `services/tenant_deletion.delete_tenant` — the tenant
database, then documents, then the control-plane rows. What this module adds is
what makes it safe to hand to a customer:

**It refuses, fail-closed, anything that is not an unused workspace.** Each
condition in `BLOCKERS` is a reason a closure must go through support instead:
records with a retention obligation or an outside effect (`TENANT_CHECKS`),
other people's accounts (another active user, a supplier-portal login), a
commercial relationship (a paid or provider-backed subscription, a partner
parent or child), operator-side state (a registered custom domain), and a
deployment that cannot keep the promises below (`DEPLOYMENT_BLOCKERS`). A check
that cannot be evaluated counts as a blocker (`records_unverifiable`), never as
a pass. Every tenant table is either guarded or named in `CLOSURE_ALLOWED` with
the reason, and `tests/test_workspace_closure.py` fails on one that is neither.

**Nothing it checked can change before the delete.** Two locks, held from the
checks to the end:

* the organisation row, `FOR UPDATE`, on the control plane. Every insert that
  references the organisation (a user, a subscription) takes `FOR KEY SHARE` on
  that row for its foreign key, and every `settings` writer takes `FOR UPDATE`
  (`tenant.lock_organization`), so none can land while the closure runs. The
  organisation's existing `subscriptions` and `users` rows are locked too,
  because an UPDATE of one (a plan change, a reactivation) does not touch the
  organisation row. The actor is re-validated under that lock. The control-plane
  rows are deleted, and the closure recorded complete, in that same transaction
  (`delete_tenant(control_session=…)`).
* `SHARE` on every guarded tenant table, in a transaction kept open until
  `DROP DATABASE … WITH (FORCE)` terminates it. No approval or payment can land
  between the check and the drop.

Two closures of one workspace serialize on a transaction-scoped advisory lock.

**It leaves a record that outlives the tenant.** The tenant's own audit log is
destroyed with its database, so: (1) an `organization.closing` row is written
there (with the request's source IP, as every audit row carries one); queued
audit writes are awaited; where audit shipping is on, the tenant's whole
unshipped trail is flushed to the write-once archive and then SEALED — an
`EXCLUSIVE` lock on `audit_log`, taken only once nothing is unshipped and held
until the drop, so no row can be committed after the last flush and lost with
the database. A failure refuses the closure. In a deployed environment with no
real archive configured the closure is refused outright. (2) a control-plane
`workspace_closures` row records who asked (id and role names), with which
proof, what was removed, and any part-way failure. That row is also the
operator's queue for the per-tenant backups, which the API cannot reach.

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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from app.api.deps import ROLE_ADMIN
from app.config import settings
from app.database import _make_tenant_url, control_session_factory, forget_tenant_engine
from app.db_engine import create_async_engine
from app.models.invoice_status import APPROVED_OR_BEYOND_STATUSES
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace_closure import (
    CLOSURE_COMPLETED,
    CLOSURE_STARTED,
    WorkspaceClosure,
)
from app.services import audit_log_shipper, tenant_deletion
from app.services.audit_dispatch import record_auth_audit_or_raise, settle_auth_audits

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blockers. Each code is stable: the API returns them in `params.blockers` and
# the SPA states each one (`frontend/src/lib/types/workspaceClosure.ts`).
# ---------------------------------------------------------------------------

# Deployment.
BLOCKER_SECOND_FACTOR_UNAVAILABLE = "second_factor_unavailable"
BLOCKER_AUDIT_ARCHIVE_UNAVAILABLE = "audit_archive_unavailable"
BLOCKER_AUDIT_QUEUE_UNVERIFIABLE = "audit_queue_unverifiable"
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
BLOCKER_APPROVED_PREAPPROVALS = "approved_preapprovals"
BLOCKER_CARD_TRANSACTIONS = "card_transactions"
BLOCKER_BANK_STATEMENTS = "bank_statements"
BLOCKER_STATEMENT_RECONCILIATIONS = "statement_reconciliations"
BLOCKER_TAX_FILINGS = "tax_filings"
BLOCKER_TAX_RECORDS = "tax_records"
BLOCKER_POSITIVE_PAY_FILES = "positive_pay_files"
BLOCKER_EINVOICE_TRANSMISSIONS = "einvoice_transmissions"
BLOCKER_PROGRESSED_PURCHASE_ORDERS = "progressed_purchase_orders"
BLOCKER_APPROVED_REQUISITIONS = "approved_requisitions"
BLOCKER_APPROVED_INTAKE_REQUESTS = "approved_intake_requests"
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

#: Invoice statuses that only exist once someone approved the invoice — the
#: named group from `app/models/invoice_status.py`, never restated here. Paired
#: with `approved_by IS NOT NULL` in the check, which also catches an approved
#: invoice whose ERP send failed (`failed` is shared with extraction failures,
#: so the status alone cannot say).
_POST_APPROVAL_STATUSES: tuple[str, ...] = tuple(str(s) for s in APPROVED_OR_BEYOND_STATUSES)

#: Purchase-order statuses that say nothing has happened to the order yet.
#: `PurchaseOrder.status` is free-form (the ERP feed writes it), so this is the
#: allow-list and anything else — received, partially received, closed — blocks.
_UNTOUCHED_PO_STATUSES = ("open", "draft", "unissued", "cancelled", "voided")


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
#: its table being held for the rest of the closure. Every OTHER tenant table is
#: in `CLOSURE_ALLOWED`, with the reason it may be deleted.
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
        BLOCKER_APPROVED_PREAPPROVALS,
        "expense_preapprovals",
        _any_row("expense_preapprovals", "status = 'approved'"),
    ),
    TenantCheck(
        BLOCKER_CARD_TRANSACTIONS,
        "corporate_card_transactions",
        _any_row("corporate_card_transactions"),
    ),
    TenantCheck(BLOCKER_BANK_STATEMENTS, "bank_statements", _any_row("bank_statements")),
    TenantCheck(
        BLOCKER_STATEMENT_RECONCILIATIONS,
        "vendor_statement_reconciliations",
        _any_row("vendor_statement_reconciliations"),
    ),
    TenantCheck(BLOCKER_TAX_FILINGS, "tax_1099_filings", _any_row("tax_1099_filings")),
    TenantCheck(BLOCKER_TAX_RECORDS, "intl_tax_records", _any_row("intl_tax_records")),
    TenantCheck(BLOCKER_POSITIVE_PAY_FILES, "positive_pay_files", _any_row("positive_pay_files")),
    TenantCheck(
        BLOCKER_EINVOICE_TRANSMISSIONS, "peppol_transmissions", _any_row("peppol_transmissions")
    ),
    TenantCheck(
        BLOCKER_PROGRESSED_PURCHASE_ORDERS,
        "purchase_orders",
        _any_row("purchase_orders", f"status NOT IN ({_quoted(_UNTOUCHED_PO_STATUSES)})"),
    ),
    TenantCheck(
        BLOCKER_APPROVED_REQUISITIONS,
        "purchase_requisitions",
        _any_row("purchase_requisitions", "status IN ('approved', 'converted')"),
    ),
    TenantCheck(
        BLOCKER_APPROVED_INTAKE_REQUESTS,
        "intake_requests",
        _any_row("intake_requests", "status IN ('approved', 'converted')"),
    ),
    TenantCheck(BLOCKER_GOODS_RECEIPTS, "goods_receipts", _any_row("goods_receipts")),
    TenantCheck(
        BLOCKER_INVENTORY_PUSHES, "commerce_inventory_syncs", _any_row("commerce_inventory_syncs")
    ),
    TenantCheck(
        BLOCKER_ACTIVE_CONTRACTS,
        "contracts",
        # `activated_at` (migration 0120) is the evidence: a contract active and
        # then cancelled still bound the parties, and its status no longer says so.
        _any_row(
            "contracts", "activated_at IS NOT NULL OR status IN ('active', 'expired', 'terminated')"
        ),
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


def _covered_by(parent: str) -> str:
    return f"Child rows of `{parent}`, whose guard decides for them."


#: Every tenant table a closure may delete without a guard of its own, with the
#: reason — the counterpart of `TENANT_CHECKS`, in the shape of
#: `tenant_deletion.CONTROL_TABLES_EXEMPT`. A tenant table must be in exactly one
#: of the two (`tests/test_workspace_closure.py`), so a new table cannot be
#: added without someone deciding whether it is a record that must be kept.
CLOSURE_ALLOWED: dict[str, str] = {
    # Children whose parent is guarded.
    "invoice_line_items": _covered_by("invoices"),
    "invoice_extraction_results": _covered_by("invoices"),
    "invoice_embeddings": _covered_by("invoices"),
    "payment_schedules": _covered_by("payments"),
    "card_rebates": _covered_by("virtual_cards"),
    "card_reveal_tokens": _covered_by("virtual_cards"),
    "bank_transactions": _covered_by("bank_statements"),
    "vendor_statement_recon_lines": _covered_by("vendor_statement_reconciliations"),
    "expenses": _covered_by("expense_reports"),
    "gr_line_items": _covered_by("goods_receipts"),
    "quality_inspections": _covered_by("goods_receipts"),
    "po_line_items": _covered_by("purchase_orders"),
    "requisition_line_items": _covered_by("purchase_requisitions"),
    "contract_line_items": _covered_by("contracts"),
    # Master and reference data: a trial plausibly holds it, and nothing in it is
    # a transaction that happened.
    "vendors": (
        "Supplier master data, typically pulled from an ERP that stays its record. "
        "Bank-detail changes that were approved block on their own."
    ),
    "gl_accounts": "The chart of accounts, typically pulled from an ERP that stays its record.",
    "entities": "The workspace's own legal-entity list.",
    "expense_policies": "Configuration, not a transaction.",
    "budgets": "Planning figures; spend against them is computed from guarded records.",
    "catalogs": "Supplier catalogue configuration.",
    "catalog_items": "Supplier catalogue entries.",
    "punchout_sessions": "Short-lived shopping sessions with a supplier's site.",
    "commerce_sku_mappings": "Configuration mapping SKUs; pushes block on their own.",
    "recurring_invoice_templates": (
        "Templates only; every invoice generated from one is guarded as an invoice."
    ),
    "workflow_definitions": "Approval-workflow configuration.",
    "workflow_versions": "Approval-workflow configuration history.",
    "workflow_instances": "Per-invoice workflow state; approvals block as invoices.",
    "workflow_steps": "Per-invoice workflow steps; approvals block as invoices.",
    "workflow_experiments": "A/B configuration for approval routing.",
    "workflow_suggestions": "Adaptive-routing suggestions, never applied facts.",
    "report_definitions": "Saved report layouts.",
    "scheduled_reports": "Report schedules.",
    "cash_plans": "Proposed cash plans; any payment they led to blocks on its own.",
    "vendor_extraction_priors": "Learned extraction hints.",
    "extraction_usage": "Metering rows for AI reads; billing is the control-plane plan.",
    # Work in progress and operational state.
    "exceptions": (
        "The exception queue. A cleared payment-blocking exception only matters once "
        "a payment exists, which blocks on its own."
    ),
    "agent_decisions": "Decision log of the exception agents over that queue.",
    "sanctions_checks": (
        "Screening results; evidence that matters once a payment exists, which blocks."
    ),
    "notifications": "In-app notifications.",
    "assistant_conversations": "Assistant chat history.",
    "assistant_messages": "Assistant chat history.",
    "supplier_chat_threads": (
        "Supplier conversations; a supplier with a portal login blocks on its own."
    ),
    "supplier_chat_messages": "Supplier conversation messages.",
    "audit_log": (
        "Shipped to the write-once archive and sealed before the drop where shipping "
        "is on; a deployed environment without a real archive refuses the closure."
    ),
}

DEPLOYMENT_BLOCKERS: tuple[str, ...] = (
    BLOCKER_SECOND_FACTOR_UNAVAILABLE,
    BLOCKER_AUDIT_ARCHIVE_UNAVAILABLE,
    BLOCKER_AUDIT_QUEUE_UNVERIFIABLE,
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
    *DEPLOYMENT_BLOCKERS,
    *CONTROL_BLOCKERS,
    *(c.code for c in TENANT_CHECKS),
    BLOCKER_RECORDS_UNVERIFIABLE,
)

#: What a closure deletes that no guard stopped, counted for the confirmation
#: panel so the irreversible action is an informed one. Codes are stable; the
#: SPA labels each (`frontend/src/lib/types/workspaceClosure.ts`).
INVENTORY_COUNTS: tuple[tuple[str, str], ...] = (
    ("invoices", "SELECT count(*) FROM invoices"),
    ("vendors", "SELECT count(*) FROM vendors"),
    ("purchase_orders", "SELECT count(*) FROM purchase_orders"),
    ("credit_memos", "SELECT count(*) FROM credit_memos"),
    ("expense_reports", "SELECT count(*) FROM expense_reports"),
    ("contracts", "SELECT count(*) FROM contracts"),
    ("exceptions", "SELECT count(*) FROM exceptions"),
)
INVENTORY_INACTIVE_USERS = "inactive_users"

#: How long the closure waits for a lock — the organisation row or a guarded
#: table — before giving up (as `records_unverifiable`). A write in flight on an
#: unused workspace is rare; one that holds a lock this long means it is in use.
LOCK_TIMEOUT = "5s"

#: How many flush-then-seal rounds before a still-growing trail refuses.
SEAL_ATTEMPTS = 3

#: How long to wait for audit writes this process has queued but not finished.
AUDIT_SETTLE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Errors the router maps to coded refusals.
# ---------------------------------------------------------------------------


class ClosureRefused(Exception):
    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__(", ".join(blockers))


class ClosureActorInvalid(Exception):
    """Under the lock, the requester is no longer an active admin of the org."""


class ClosureInProgress(Exception):
    """Another closure of this organisation holds the lock, or finished it."""


class ClosureAuditUnavailable(Exception):
    """The closing audit row could not be written, shipped or sealed; nothing
    was deleted."""


class ClosureIncomplete(Exception):
    """The deletion began and failed part-way; a retry finishes it."""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def real_audit_archive_configured() -> bool:
    """Shipping is on and every configured sink is a real one (`mock` acks and
    keeps nothing, so it does not count)."""
    providers = audit_log_shipper._parse_providers(settings.audit_shipping_providers)
    return bool(
        settings.audit_shipping_enabled and providers and all(p != "mock" for p in providers)
    )


def deployment_blockers() -> list[str]:
    """What this deployment cannot promise, whatever the workspace holds.

    Local dev and CI are exempt, as everywhere (guard rail 7): no second factor
    and no archive exist there to be had.
    """
    if not settings.is_deployed:
        return []
    found: list[str] = []
    # `require_sensitive_step_up` refuses every call there; say so up front
    # rather than offer a control that cannot succeed.
    if not settings.mfa_enabled:
        found.append(BLOCKER_SECOND_FACTOR_UNAVAILABLE)
    # Without a real write-once sink, the tenant's audit trail would go with
    # its database, never having been anywhere else.
    if not real_audit_archive_configured():
        found.append(BLOCKER_AUDIT_ARCHIVE_UNAVAILABLE)
    # In `lambda` mode an audit row can still be in SQS, not yet in the
    # tenant's table, when the flush runs; nothing can see or drain one
    # tenant's messages from the queue, so it would land on a dropped database.
    if settings.audit_mode == "lambda":
        found.append(BLOCKER_AUDIT_QUEUE_UNVERIFIABLE)
    return found


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


@dataclass
class Preflight:
    blockers: list[str]
    #: Non-zero counts of what the closure would delete, by `INVENTORY_COUNTS`
    #: code plus `inactive_users`.
    will_delete: dict[str, int]


async def preflight(
    control_db: AsyncSession,
    tenant_db: AsyncSession,
    org: Organization,
    actor_id: uuid.UUID,
) -> Preflight:
    """Every blocker and what would be deleted, unlocked — what the page shows
    before anything is asked."""
    found = deployment_blockers()
    found += await control_blockers(control_db, org, actor_id)
    found += await tenant_blockers(tenant_db)
    if BLOCKER_RECORDS_UNVERIFIABLE in found:
        # The failed statement aborted the request's tenant transaction; leave
        # the session usable for the dependency's teardown.
        await tenant_db.rollback()
        return Preflight(blockers=found, will_delete={})

    will_delete: dict[str, int] = {}
    try:
        for code, sql in INVENTORY_COUNTS:
            count = int((await tenant_db.execute(text(sql))).scalar() or 0)
            if count:
                will_delete[code] = count
    except Exception as exc:  # noqa: BLE001 — an uncounted closure is not an informed one
        logger.warning(
            "[workspace-close] the deletion inventory could not be counted: %s",
            exc.__class__.__name__,
        )
        await tenant_db.rollback()
        return Preflight(blockers=[*found, BLOCKER_RECORDS_UNVERIFIABLE], will_delete={})
    inactive = int(
        (
            await control_db.execute(
                text(
                    "SELECT count(*) FROM users WHERE organization_id = :org "
                    "AND NOT is_active AND id <> :actor"
                ),
                {"org": org.id, "actor": actor_id},
            )
        ).scalar()
        or 0
    )
    if inactive:
        will_delete[INVENTORY_INACTIVE_USERS] = inactive
    return Preflight(blockers=found, will_delete=will_delete)


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


async def _record_started(
    org_id: uuid.UUID,
    slug: str,
    actor_id: uuid.UUID,
    proof: str,
    roles: list[str],
) -> None:
    """Committed on its own, BEFORE the deletion: if the run dies part-way, the
    row is what lets a retry resume and what an operator sees."""
    async with control_session_factory() as s:
        stmt = insert(WorkspaceClosure).values(
            id=uuid.uuid4(),
            organization_id=org_id,
            slug=slug,
            requested_by_user_id=actor_id,
            requested_by_roles=roles,
            step_up_proof=proof,
            status=CLOSURE_STARTED,
        )
        # A retry after a partial failure re-records who resumed it.
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkspaceClosure.organization_id],
            set_={
                "requested_by_user_id": actor_id,
                "requested_by_roles": roles,
                "step_up_proof": proof,
                "status": CLOSURE_STARTED,
            },
        )
        await s.execute(stmt)
        await s.commit()


async def record_failure(org_id: uuid.UUID, exc: BaseException) -> None:
    """Note a part-way failure on the closure row — the one place it survives
    when the tenant's own log is already gone. The class name only."""
    try:
        async with control_session_factory() as s:
            await s.execute(
                update(WorkspaceClosure)
                .where(WorkspaceClosure.organization_id == org_id)
                .values(last_failure=exc.__class__.__name__[:64], last_failure_at=datetime.now(UTC))
            )
            await s.commit()
    except Exception as record_exc:  # noqa: BLE001 — the log line below still stands
        logger.error(
            "[workspace-close] org %s: failure not recorded on the closure row (%s)",
            org_id,
            record_exc.__class__.__name__,
        )


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
            last_failure=None,
            last_failure_at=None,
        )
    )


async def _actor_roles(session: AsyncSession, org_id: uuid.UUID, actor_id: uuid.UUID) -> list[str]:
    """Re-validate the requester under the lock and return their role names.

    The request authenticated them moments ago, but the lock is what makes the
    answer final: a deactivation, a demotion or a deletion that landed in
    between is refused here rather than acted on.
    """
    actor = (
        await session.execute(
            select(User).options(selectinload(User.roles)).where(User.id == actor_id)
        )
    ).scalar_one_or_none()
    if actor is None or not actor.is_active or actor.organization_id != org_id:
        raise ClosureActorInvalid
    roles = sorted({r.name for r in (actor.roles or [])})
    if ROLE_ADMIN not in roles:
        raise ClosureActorInvalid
    return roles


async def _write_closing_audit(
    org_id: uuid.UUID, actor_id: uuid.UUID, proof: str, ip: str | None, roles: list[str]
) -> None:
    """The tenant-side record of the closure, written synchronously in every
    audit mode. Carries the source IP the way the auth rows do."""
    try:
        await record_auth_audit_or_raise(
            organization_id=org_id,
            actor_id=actor_id,
            action="organization.closing",
            entity_type="organization",
            entity_id=org_id,
            details={"step_up_proof": proof, "ip": ip, "roles": roles},
        )
    except Exception as exc:
        # Ids and the class only: the exception text can echo the row it failed
        # to write. Nothing has been deleted at this point, which is the point
        # of writing it first — the router answers 503 and the admin retries.
        logger.warning(
            "[workspace-close] org %s: closing audit not recorded (%s); nothing deleted",
            org_id,
            exc.__class__.__name__,
        )
        raise ClosureAuditUnavailable from exc


async def _ship_and_seal(org_id: uuid.UUID, db_name: str, engine) -> AsyncConnection | None:
    """Ship the whole trail, then lock `audit_log` against any further write.

    Returns the connection holding the `EXCLUSIVE` lock — the caller keeps it
    open until `DROP DATABASE … WITH (FORCE)` ends it — or None when shipping is
    off (local dev; a deployed environment never gets here without a real
    archive, `deployment_blockers`). Rows this process still has queued are
    awaited first; a row another writer commits between the flush and the lock
    is seen under the lock, the lock is released and the round repeats, so the
    database is never dropped with a committed row the archive lacks. A write
    attempted after the seal waits on it and fails with the database, which
    `audit_dispatch` reports at ERROR — never a silent loss of a committed row.
    """
    if not settings.audit_shipping_enabled:
        return None
    await settle_auth_audits(timeout=AUDIT_SETTLE_SECONDS)
    for _ in range(SEAL_ATTEMPTS):
        try:
            await audit_log_shipper.flush_tenant(db_name)
        except Exception as exc:
            logger.warning(
                "[workspace-close] org %s: audit trail not shipped (%s); nothing deleted",
                org_id,
                exc.__class__.__name__,
            )
            raise ClosureAuditUnavailable from exc
        seal = await engine.connect()
        try:
            await seal.begin()
            await seal.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            await seal.execute(text("LOCK TABLE audit_log IN EXCLUSIVE MODE"))
            unshipped = (
                await seal.execute(text("SELECT count(*) FROM audit_log WHERE shipped_at IS NULL"))
            ).scalar()
        except Exception as exc:
            await seal.close()
            logger.warning(
                "[workspace-close] org %s: audit trail not sealed (%s); nothing deleted",
                org_id,
                exc.__class__.__name__,
            )
            raise ClosureAuditUnavailable from exc
        if not unshipped:
            return seal
        await seal.close()  # releases the lock so the next flush can stamp rows
    logger.warning(
        "[workspace-close] org %s: audit trail still growing after %d rounds; nothing deleted",
        org_id,
        SEAL_ATTEMPTS,
    )
    raise ClosureAuditUnavailable


async def close_workspace(
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    step_up_proof: str,
    ip: str | None = None,
) -> ClosureOutcome:
    """Close one workspace: lock, guard, record, delete. See the module docstring.

    Raises `ClosureRefused`, `ClosureActorInvalid`, `ClosureInProgress`,
    `ClosureAuditUnavailable` or `ClosureIncomplete`; on success the
    organisation, its users and its database no longer exist, and its closure
    row reads `completed`.
    """
    async with control_session_factory() as session:
        try:
            outcome, db_name, user_ids = await _close_locked(
                session,
                org_id=org_id,
                actor_id=actor_id,
                step_up_proof=step_up_proof,
                ip=ip,
            )
            await session.commit()
        except ClosureIncomplete as exc:
            await session.rollback()
            # The tenant's own log may already be gone: the closure row and a
            # structured, id-only log line are where this failure is recorded.
            await record_failure(org_id, exc.__cause__ or exc)
            logger.error(
                "[workspace-close] org %s: closure by user %s incomplete (%s); retry resumes",
                org_id,
                actor_id,
                (exc.__cause__ or exc).__class__.__name__,
            )
            raise
        except BaseException:
            await session.rollback()
            raise

    # After the commit, and never a reason to report failure: the workspace is
    # gone. Stale state the deleted accounts left outside the database.
    await _forget_runtime_state(org_id, db_name, user_ids)
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


async def _forget_runtime_state(org_id: uuid.UUID, db_name: str, user_ids: list[uuid.UUID]) -> None:
    """Drop what the deleted workspace left in process memory and Redis.

    The tokens and links are already inert — every one resolves to a user row
    that no longer exists — but session metadata (IP, device label) and live
    reset links for deleted accounts should not linger until their TTLs.
    Best-effort: the closure has committed, so a failure here is logged, never
    reported as one.
    """
    from app.services import credential_cleanup

    try:
        await forget_tenant_engine(db_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[workspace-close] org %s: cached engine not disposed (%s)",
            org_id,
            exc.__class__.__name__,
        )
    try:
        await credential_cleanup.forget_users(user_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[workspace-close] org %s: session keys of %d deleted user(s) not cleared (%s)",
            org_id,
            len(user_ids),
            exc.__class__.__name__,
        )


async def _close_locked(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    step_up_proof: str,
    ip: str | None,
) -> tuple[ClosureOutcome, str, list[uuid.UUID]]:
    """Everything under the organisation-row lock; returns the outcome, the
    dropped database's name and the deleted users' ids. The caller commits."""
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
        user_ids = [
            row[0]
            for row in (
                await session.execute(
                    text("SELECT id FROM users WHERE organization_id = :org FOR UPDATE"),
                    {"org": org_id},
                )
            ).all()
        ]
        await session.execute(
            text("SELECT 1 FROM subscriptions WHERE organization_id = :org FOR UPDATE"),
            {"org": org_id},
        )
    except Exception as exc:  # noqa: BLE001 — a lock we cannot take is a refusal
        logger.warning(
            "[workspace-close] org %s: organisation rows not lockable (%s)",
            org_id,
            exc.__class__.__name__,
        )
        raise ClosureRefused([BLOCKER_RECORDS_UNVERIFIABLE]) from exc
    if org is None:
        # Gone between the request's own lookup and this lock: another
        # closure (or an operator) finished it.
        raise ClosureInProgress
    slug, db_name = org.slug, org.db_name

    roles = await _actor_roles(session, org_id, actor_id)
    blockers = deployment_blockers() + await control_blockers(session, org, actor_id)
    if blockers:
        raise ClosureRefused(blockers)

    if not await _database_exists(db_name) and await _started_closure_exists(session, org_id):
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
            ip=ip,
            roles=roles,
        )

    await _record_completed(session, org_id, result)
    outcome = ClosureOutcome(
        organization_id=org_id,
        slug=slug,
        documents_removed=result.objects_deleted,
        database_dropped=result.database_dropped,
        control_rows_removed=sum(result.control_rows_deleted.values()),
    )
    return outcome, db_name, user_ids


async def _guard_and_delete(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    slug: str,
    db_name: str,
    actor_id: uuid.UUID,
    step_up_proof: str,
    ip: str | None,
    roles: list[str],
) -> tenant_deletion.TenantDeletionResult:
    """Re-check under locks held until the database drop, then delete."""
    engine = create_async_engine(_make_tenant_url(db_name), poolclass=NullPool)
    held: list[AsyncConnection] = []
    try:
        try:
            conn = await engine.connect()
            held.append(conn)
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
        await _write_closing_audit(org_id, actor_id, step_up_proof, ip, roles)
        seal = await _ship_and_seal(org_id, db_name, engine)
        if seal is not None:
            held.append(seal)
        await _record_started(org_id, slug, actor_id, step_up_proof, roles)

        # Every lock is still held here. `DROP DATABASE … WITH (FORCE)` inside
        # the deletion terminates these very connections, which is what
        # releases them — there is no moment between the check and the drop
        # when a guarded row, or an audit row, could be written.
        return await _delete(slug, session)
    finally:
        for conn in held:
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
        raise ClosureIncomplete from exc


# ---------------------------------------------------------------------------
# The operator's half: backups and unfinished closures (scripts/workspace_closures.py)
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


async def unfinished_closures() -> list[WorkspaceClosure]:
    """Closures that began and have not finished while their workspace still
    exists — typically one whose database is already dropped and whose admin
    has not retried. The operator finishes them (`deploy/remove-tenant.sh`)."""
    org_exists = exists().where(Organization.id == WorkspaceClosure.organization_id)
    async with control_session_factory() as s:
        return list(
            (
                await s.execute(
                    select(WorkspaceClosure)
                    .where(WorkspaceClosure.status == CLOSURE_STARTED, org_exists)
                    .order_by(WorkspaceClosure.started_at.asc())
                )
            )
            .scalars()
            .all()
        )


async def mark_backups_cleared(organization_id: uuid.UUID, *, operator: str) -> int:
    """Record that a closed workspace's backups are gone, and who removed them.
    Returns rows marked.

    Keyed by organisation id, not slug: a slug becomes reusable after its
    cooldown (`utils/slug.ensure_slug_not_held`), and the id is the one thing
    two workspaces that shared a name never share. Only a closure whose
    organisation is gone is marked — a closure still running has backups nobody
    removed. `operator` is a handle or ticket reference, never an address.
    """
    operator = operator.strip()
    if not operator:
        raise ValueError("an operator identifier is required")
    org_gone = ~exists().where(Organization.id == WorkspaceClosure.organization_id)
    async with control_session_factory() as s:
        result = await s.execute(
            update(WorkspaceClosure)
            .where(
                WorkspaceClosure.organization_id == organization_id,
                WorkspaceClosure.backups_cleared_at.is_(None),
                org_gone,
            )
            .values(backups_cleared_at=datetime.now(UTC), backups_cleared_by=operator[:100])
        )
        await s.commit()
        return result.rowcount or 0
