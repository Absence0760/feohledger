"""ERP status sync: learn from the ERP what happened to the bills we pushed.

A pushed invoice rests at ``sent_to_erp`` while the ERP holds the bill and pays
it (docs/decisions.md §303). Two things can tell us what the ERP did with it:
the ERP webhook (``api/erp_webhook``), which needs the ERP to call us with our
normalized, signed body — Xero's native webhooks can't, and neither can a
laptop behind NAT — and this module, which ASKS the ERP through the adapter's
``get_invoice_status``. Polling is the local-first path (guard rail 7); the
webhook stays the real-time one.

Whatever the source, the report is applied by ``services/erp_reported_status``
— the same functions the webhook calls — so the two can never disagree:

* ``paid`` → recorded as a payment made outside FeohLedger (a ``completed``
  ``provider="external"`` Payment, the invoice walked to ``paid``); a refusal
  opens the blocking ``payment_reconciliation`` exception;
* ``cancelled`` → an ``erp_reconciliation`` exception (money may be in flight
  ERP-side; the invoice is never transitioned);
* ``settled_no_payment`` → an ``erp_reconciliation`` exception: the ERP shows
  nothing owing but records no payment (a credit note, a write-off), so there
  is nothing honest to record and a person decides;
* anything else (``open``, ``partially_paid``, ``draft``, ``unknown``) → nothing.
  Partial payments are not recorded (``external_payment`` refuses them).

**A sweep never moves money.** It records what the ERP already did; it never
creates a payment run, dispatches to a rail or calls the ERP to change anything.

Shape (backend/docs/background-sweeps.md): every tenant with ``settings.erp``,
keyset-paged over invoices at ``sent_to_erp`` / ``posted_in_erp`` that carry
an ERP reference, ordered by id. Each invoice is polled with NO lock and no open
transaction (an ERP round trip must not hold a row the request path needs),
then locked, re-checked and applied in its own transaction. One invoice's
failure is counted (``invoice_failures``) and never halts the tenant; one
tenant's failure is counted (``failures``) and never halts the sweep.

**Idempotent across ticks.** A recorded invoice leaves the candidate set
(``paid``). A refused record re-tries each tick — so it lands by itself once a
person clears the cause — and its exception is opened once (the open-exception
dedup). A flag (``cancelled`` / ``settled_no_payment``) is raised once per
status the ERP reports, remembered on ``WorkflowInstance.state_data``, so a
person resolving it is not answered by the same flag on the next tick.

**Only the connection that holds the bill is asked.** The push records the
adapter key and ERP company it posted to (``services/erp.ERP_ADAPTER_KEY`` /
``ERP_REALM_KEY``). An invoice is polled only while the tenant is connected to
that same adapter and company: an ERP id means nothing in another company, and
QuickBooks reuses small integers across companies, so asking the wrong one
would read another bill's status (decisions §284). A row without those keys
(pushed before they existed) is skipped for the same reason.

Today the ERP's id is read from ``WorkflowInstance.state_data["erp_reference"]``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import WorkflowInstance
from app.services.erp_adapters.base import ErpInvoiceStatus

logger = logging.getLogger(__name__)

#: Statuses of an invoice the ERP holds and has not yet reported paid.
#: ``sent_to_erp`` is where a push rests; ``posted_in_erp`` is still read, for an
#: invoice an earlier webhook moved there.
AWAITING_ERP_OUTCOME_STATUSES = (InvoiceStatus.sent_to_erp, InvoiceStatus.posted_in_erp)

#: ``WorkflowInstance.state_data`` key: the last ERP status this module raised a
#: flag for, so the same report is flagged once, not once per tick.
FLAGGED_STATUS_KEY = "erp_status_flagged"

#: The ``erp_reference`` the push records when the adapter returned no id.
_NO_REFERENCE = "UNKNOWN"

#: What the event-id slot of an exception description names for a poll.
_POLL_EVENT = "status-poll"


class SyncOutcome(StrEnum):
    """What applying one invoice's ERP status did. PII-free."""

    recorded_paid = "recorded_paid"
    refused = "refused"  # the ERP said paid; recording was refused and flagged
    flagged = "flagged"  # cancelled / settled_no_payment, flagged for a person
    unchanged = "unchanged"  # nothing to apply (open, partial, already flagged…)
    not_awaiting = "not_awaiting"  # the invoice is not waiting on the ERP
    other_connection = "other_connection"  # pushed through another ERP / company


@dataclass(frozen=True)
class ErpConnection:
    """The tenant's current ERP connection, resolved once per tenant."""

    adapter: object
    adapter_key: str
    realm: str | None


@dataclass
class ErpStatusSyncResult:
    tenants_scanned: int = 0
    invoices_polled: int = 0
    recorded_paid: int = 0
    refused: int = 0
    flagged: int = 0
    #: Invoices whose poll or apply raised — counted apart from `failures`.
    invoice_failures: int = 0
    #: Tenants whose sweep could not run (credentials, adapter, DB).
    failures: int = 0


def tenant_sessionmaker(tenant_db_name: str) -> async_sessionmaker:
    """A session maker on the tenant's engine, valid on the caller's loop.

    A module-level seam: tests point it at the harness's per-loop engines.
    """
    from app.database import get_tenant_engine

    return async_sessionmaker(get_tenant_engine(tenant_db_name), expire_on_commit=False)


def resolve_connection(erp_config: dict) -> ErpConnection:
    """The adapter for ``erp_config`` (the resolved ``settings.erp``), with the
    adapter key and company the push would record. Raises
    ``UnknownErpAdapterError`` for an adapter we don't have."""
    from app.services.erp_adapters import get_erp_adapter
    from app.services.erp_adapters.dispatcher import resolve_adapter_key
    from app.services.erp_link_realm import connection_realm

    return ErpConnection(
        adapter=get_erp_adapter(erp_config),
        adapter_key=resolve_adapter_key(erp_config),
        realm=connection_realm(erp_config),
    )


def pollable_reference(state_data: dict | None, connection: ErpConnection) -> str | None:
    """The ERP id to poll for this invoice under ``connection``, or None.

    None when the push recorded no usable id, or when it posted through another
    adapter or ERP company than the one the tenant is connected to now.
    """
    from app.services.erp import ERP_ADAPTER_KEY, ERP_REALM_KEY

    state = state_data or {}
    reference = state.get("erp_reference")
    if not reference or reference == _NO_REFERENCE:
        return None
    if ERP_ADAPTER_KEY not in state or ERP_REALM_KEY not in state:
        return None
    if state[ERP_ADAPTER_KEY] != connection.adapter_key:
        return None
    if state[ERP_REALM_KEY] != connection.realm:
        return None
    return str(reference)


async def poll_status(connection: ErpConnection, reference: str) -> ErpInvoiceStatus:
    """Ask the ERP. No lock and no transaction may be open around this call."""
    status = await connection.adapter.get_invoice_status(reference)
    try:
        return ErpInvoiceStatus(status)
    except ValueError:
        return ErpInvoiceStatus.unknown


async def apply_status(
    db: AsyncSession,
    *,
    org: Organization,
    invoice_id: uuid.UUID,
    reference: str,
    erp_status: ErpInvoiceStatus,
    adapter_key: str,
) -> SyncOutcome:
    """Apply one polled status to one invoice. The caller commits.

    Locks the invoice and RE-CHECKS what the candidate query and the poll
    assumed — it still awaits the ERP, under the same ERP reference — because
    a webhook, a person or another replica may have moved it during the poll.
    """
    from app.services.erp_reported_status import (
        open_erp_reconciliation_exception,
        raise_erp_cancelled_exception,
        record_erp_reported_payment,
    )
    from app.services.workflow_engine import get_invoice_for_update, get_workflow_instance

    if erp_status not in (
        ErpInvoiceStatus.paid,
        ErpInvoiceStatus.cancelled,
        ErpInvoiceStatus.settled_no_payment,
    ):
        return SyncOutcome.unchanged

    invoice = await get_invoice_for_update(db, invoice_id)
    instance = await get_workflow_instance(db, invoice.id)
    state = (instance.state_data if instance is not None else None) or {}
    if (
        invoice.status not in AWAITING_ERP_OUTCOME_STATUSES
        or str(state.get("erp_reference") or "") != reference
    ):
        return SyncOutcome.not_awaiting

    if erp_status is ErpInvoiceStatus.paid:
        recorded = await record_erp_reported_payment(
            db,
            invoice,
            org=org,
            erp_type=adapter_key,
            erp_document_id=reference,
            event_id=_POLL_EVENT,
        )
        return SyncOutcome.recorded_paid if recorded else SyncOutcome.refused

    # Flag-once: a person resolving the flag must not be answered by the same
    # flag on the next tick. A different status later is a new report.
    if state.get(FLAGGED_STATUS_KEY) == erp_status.value:
        return SyncOutcome.unchanged
    if erp_status is ErpInvoiceStatus.cancelled:
        await raise_erp_cancelled_exception(
            db,
            invoice,
            org_id=org.id,
            erp_type=adapter_key,
            erp_status=erp_status.value,
            erp_document_id=reference,
            event_id=_POLL_EVENT,
        )
    else:
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=org.id,
            description=(
                f"The ERP ({adapter_key}) shows nothing owing on this bill but records "
                f"no payment against it — it may have been settled by a credit note or "
                f"written off. FeohLedger has recorded nothing. If it was paid, record "
                f"it via POST /api/payments/record-outside "
                f"(erp_document_id={reference}, event={_POLL_EVENT})."
            ),
        )
    if instance is not None:
        # A NEW dict: `state_data` is plain JSONB, so an in-place edit is lost.
        instance.state_data = {**state, FLAGGED_STATUS_KEY: erp_status.value}
    return SyncOutcome.flagged


async def candidate_page(
    db: AsyncSession, *, after: uuid.UUID | None, limit: int
) -> list[tuple[uuid.UUID, dict | None]]:
    """One keyset page of ``(invoice id, state_data)`` awaiting an ERP outcome."""
    query = (
        select(Invoice.id, WorkflowInstance.state_data)
        .join(WorkflowInstance, WorkflowInstance.invoice_id == Invoice.id)
        .where(
            Invoice.status.in_(AWAITING_ERP_OUTCOME_STATUSES),
            WorkflowInstance.state_data["erp_reference"].astext.isnot(None),
        )
        .order_by(Invoice.id)
        .limit(limit)
    )
    if after is not None:
        query = query.where(Invoice.id > after)
    return [(row[0], row[1]) for row in (await db.execute(query)).all()]


async def sync_tenant(
    org: Organization, connection: ErpConnection, result: ErpStatusSyncResult
) -> None:
    """Poll and apply every invoice of one tenant awaiting an ERP outcome.

    Pages until the tenant is exhausted: a bill the ERP has not paid stays a
    candidate, so a capped page would re-serve the same lowest ids forever.
    """
    maker = tenant_sessionmaker(org.db_name)
    page_size = max(1, settings.erp_status_sync_batch_size)
    after: uuid.UUID | None = None
    while True:
        async with maker() as db:
            page = await candidate_page(db, after=after, limit=page_size)
        if not page:
            return
        after = page[-1][0]
        for invoice_id, state_data in page:
            reference = pollable_reference(state_data, connection)
            if reference is None:
                continue
            try:
                erp_status = await poll_status(connection, reference)
                result.invoices_polled += 1
                async with maker() as db:
                    outcome = await apply_status(
                        db,
                        org=org,
                        invoice_id=invoice_id,
                        reference=reference,
                        erp_status=erp_status,
                        adapter_key=connection.adapter_key,
                    )
                    await db.commit()
            except Exception as exc:  # noqa: BLE001 — one invoice must not halt the tenant
                logger.warning(
                    "[erp-status-sync] invoice %s failed: %s", invoice_id, type(exc).__name__
                )
                result.invoice_failures += 1
                continue
            if outcome is SyncOutcome.recorded_paid:
                result.recorded_paid += 1
            elif outcome is SyncOutcome.refused:
                result.refused += 1
            elif outcome is SyncOutcome.flagged:
                result.flagged += 1
        if len(page) < page_size:
            return


async def run_erp_status_sync_once() -> ErpStatusSyncResult:
    """One pass over every tenant with an ERP configured."""
    from app.database import control_session_factory
    from app.services.credential_crypto import CredentialCryptoError
    from app.services.provider_credentials import provider_config

    result = ErpStatusSyncResult()
    async with control_session_factory() as ctrl:
        orgs = list((await ctrl.execute(select(Organization))).scalars().all())
        tenants: list[tuple[Organization, dict | None, bool]] = []
        for org in orgs:
            if not (org.settings or {}).get("erp"):
                continue
            try:
                tenants.append((org, await provider_config(org, "erp", db=ctrl), True))
            except CredentialCryptoError:
                tenants.append((org, None, False))
    for org, erp_config, credentials_ok in tenants:
        result.tenants_scanned += 1
        if not credentials_ok or not erp_config:
            # Without the sealed secrets there is nothing to ask the ERP with —
            # and falling back to `mock` would answer "open" for every bill.
            logger.warning("[erp-status-sync] tenant %s: ERP credentials unavailable", org.slug)
            result.failures += 1
            continue
        try:
            connection = resolve_connection(erp_config)
            await sync_tenant(org, connection, result)
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            logger.warning("[erp-status-sync] tenant %s failed: %s", org.slug, type(exc).__name__)
            result.failures += 1
    return result


async def run_erp_status_sync_loop() -> None:
    """Long-lived loop started in ``main.lifespan`` when FEOH_ERP_STATUS_SYNC_ENABLED."""
    from app.services.sweep_health import SWEEP_ERP_STATUS_SYNC, run_sweep_loop

    await run_sweep_loop(
        SWEEP_ERP_STATUS_SYNC,
        lambda: run_erp_status_sync_once(),
        interval_seconds=settings.erp_status_sync_interval_seconds,
        log=logger,
        log_prefix="[erp-status-sync]",
    )
