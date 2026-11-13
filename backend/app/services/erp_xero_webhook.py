"""Xero's invoice webhooks: a bill changed in Xero reaches FeohLedger now,
not at the next status-sync tick (decisions §392).

``api/erp_webhook.xero_webhook`` verifies the delivery (``x-xero-signature``,
base64 HMAC-SHA256 of the raw body under the platform app's ONE webhook key),
then hands its events here. Each INVOICE event names an invoice by its
``resourceId`` and the organisation by its ``tenantId``; nothing else is read
from it. The organisation picks the tenant through the control-plane index on
the stored connection (``erp_status_sync.org_for_company``, the QuickBooks
webhook's own lookup), and the bill is read back from Xero and applied by
``erp_status_sync.sync_bills``, the sweep's per-bill path, so the webhook and
the sweep can never disagree about what a bill means. ``sync_bills`` reads
only a bill some invoice here was pushed as, in that organisation, so an
event about a sales invoice or a hand-keyed bill costs no Xero call.

Xero publishes no event id. An event is identified by everything it states
about itself (organisation, category, type, resource, time), which a
redelivery repeats exactly, so a replay is dropped by the same claim the other
webhooks use (``webhook_security.is_event_already_processed``).

The work runs after the response: Xero wants an answer within five seconds,
and the sweep is the backstop for anything a failed event loses.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

XERO_PROVIDER = "xero"
#: The dedup namespace (``webhook:event:erp:xero:<key>``).
DEDUP_PROVIDER = "erp:xero"
#: The only category whose events name a bill (an ACCPAY invoice).
INVOICE_CATEGORY = "INVOICE"
#: Longest organisation / resource id accepted (both are GUIDs at Xero).
_MAX_ID = 128
#: The fields an event identifies itself by, in key order.
_IDENTITY_FIELDS = ("tenantId", "eventCategory", "eventType", "resourceId", "eventDateUtc")


@dataclass(frozen=True)
class XeroEvent:
    #: The dedup key (:func:`event_key`), Xero having no event id.
    event_key: str
    #: Xero's organisation id: ``external_tenant_id`` on the connection.
    tenant_id: str
    #: The ``InvoiceID``: a bill's ``erp_reference`` here.
    invoice_id: str


def event_key(event: dict) -> str:
    """A stable id for one Xero event: a digest of every field it states
    about itself. A redelivery repeats the event verbatim, so it hashes the
    same; two changes to one bill differ at least in ``eventDateUtc``."""
    parts = [str(event.get(name) or "").strip() for name in _IDENTITY_FIELDS]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def parse_xero_events(body: object) -> list[XeroEvent]:
    """The INVOICE events of a Xero delivery, each once.

    The body is ``{"events": [...], "firstEventSequence", "lastEventSequence",
    "entropy"}``; the intent-to-receive check sends it with no events. An
    event of another category, for another kind of tenant, or with no
    organisation or resource id is dropped. The event is never trusted for the
    bill's state: the bill is read back from Xero."""
    if not isinstance(body, dict) or not isinstance(body.get("events"), list):
        return []
    out: list[XeroEvent] = []
    seen: set[str] = set()
    for event in body["events"]:
        if not isinstance(event, dict):
            continue
        if str(event.get("eventCategory") or "").upper() != INVOICE_CATEGORY:
            continue
        tenant_type = event.get("tenantType")
        if tenant_type is not None and str(tenant_type).upper() != "ORGANISATION":
            continue
        tenant = str(event.get("tenantId") or "").strip()
        resource = str(event.get("resourceId") or "").strip()
        # The time is what tells two changes to one bill apart (``event_key``):
        # without it a later change would be deduped as this one.
        moment = str(event.get("eventDateUtc") or "").strip()
        if not (tenant and resource and moment) or max(len(tenant), len(resource)) > _MAX_ID:
            continue
        key = event_key(event)
        if key in seen:
            continue
        seen.add(key)
        out.append(XeroEvent(key, tenant, resource))
    return out


async def claim_xero_events(events: list[XeroEvent]) -> list[XeroEvent]:
    """The events not seen before, each claimed by its key. Called by the
    route BEFORE it answers, so a dedup store that cannot be reached raises
    there and becomes a 503 Xero redelivers, never a 200 for unclaimed work."""
    from app.services.webhook_security import is_event_already_processed, release_event_claim

    fresh: list[XeroEvent] = []
    try:
        for event in events:
            if not await is_event_already_processed(DEDUP_PROVIDER, event.event_key):
                fresh.append(event)
    except Exception:
        # The route answers 503 and dispatches nothing, so the claims already
        # made here guard no work: released, Xero's redelivery is not deduped
        # away (best effort; the sweep is the backstop if a release fails).
        for event in fresh:
            await release_event_claim(DEDUP_PROVIDER, event.event_key)
        raise
    return fresh


async def process_xero_event(event: XeroEvent) -> None:
    """Apply one event. Raises on our own failure (the caller releases the
    claim; Xero already has its 200, so the sweep is what picks it up).

    Every refusal is a decision, not a failure, and does nothing: an
    organisation no tenant is connected to (or one whose tenant has moved to
    another ERP), and a tenant the sweep itself would skip (status sync off,
    a connection that needs reconnecting): ``status_sync_now_refusal``, the
    rule the on-request pass and the sweep share."""
    from app.services import erp_status_sync

    org = await erp_status_sync.org_for_company(XERO_PROVIDER, event.tenant_id)
    if org is None:
        return
    if await erp_status_sync.status_sync_now_refusal(org) is not None:
        # A decision, so the claim stands: the sweep reads the bill when the
        # tenant is synced again (its mark has not moved past the change).
        return
    await erp_status_sync.sync_bills(org, [event.invoice_id], event_id=event.event_key)


async def process_xero_events(events: list[XeroEvent]) -> int:
    """Apply each claimed event; returns how many failed. A failed event's
    claim is released. Never raises: one event never stops the rest."""
    from app.services.webhook_security import release_event_claim

    failures = 0
    for event in events:
        try:
            await process_xero_event(event)
        except Exception as exc:  # noqa: BLE001 — one event never stops the rest
            failures += 1
            await release_event_claim(DEDUP_PROVIDER, event.event_key)
            logger.warning(
                "[xero-webhook] event for organisation %s failed: %s",
                event.tenant_id,
                exc.__class__.__name__,
            )
    return failures


#: Strong references to in-flight webhook tasks (asyncio holds only a weak one).
_webhook_tasks: set[asyncio.Task] = set()


def dispatch_xero_events(events: list[XeroEvent]) -> None:
    """Process a verified delivery after the response has gone back (the
    ``erp_status_sync.dispatch_qbo_events`` shape)."""
    if not events:
        return
    task = asyncio.create_task(process_xero_events(events), name="xero-webhook")
    _webhook_tasks.add(task)
    task.add_done_callback(_webhook_tasks.discard)
