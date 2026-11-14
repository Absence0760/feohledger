"""Merge.dev's ``Invoice`` webhooks: a bill changed in the ERP behind Merge
reaches FeohLedger as it happens (decisions §399).

``api/erp_webhook.merge_webhook`` resolves the tenant from the URL it
registered in its own Merge dashboard (``/api/erp/webhook/merge_dev/{slug}``),
verifies the delivery against that tenant's Merge signature key
(``X-Merge-Webhook-Signature``, the base64url HMAC-SHA256 of the raw body), and
only then hands the body here. Each tenant connects its OWN Merge organisation
(its own API key, account token and signature key), so a delivery signed with
one tenant's key can never be applied to another tenant: the URL picks whose key
verifies it.

Merge's payload is ``{hook, linked_account, data}``, ``data`` being the
Invoice. Only its id is used: the bill is read back from Merge
(``MergeDevAdapter.bill_report``), mapped by ``merge_dev.merge_bill_report``
and applied by ``erp_status_sync.apply_bill_report`` — the reconciler
QuickBooks' and Xero's status sync use — so a Merge bill means the same thing
here as a bill read by the sweep. A Merge bill carries no marker of ours; it is matched
by the invoice's ``erp_reference`` within the realm the push recorded, and a
bill no invoice was pushed as matches nothing.

Merge publishes no per-delivery id (``hook.id`` names the webhook
subscription, the same on every delivery). A change is identified by the bill
and its ``modified_at``, which a redelivery repeats and a later change moves,
so a replay is dropped by the same claim the other webhooks use
(``webhook_security.is_event_already_processed``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.erp_adapters.base import ErpBillReport, ErpBillState

MERGE_PROVIDER = "merge_dev"
#: The dedup namespace (``webhook:event:erp:merge_dev:<key>``).
DEDUP_PROVIDER = "erp:merge_dev"
#: The only common model whose events name a bill.
INVOICE_MODEL = "Invoice"
#: ``{model}.removed``: the record was deleted in the ERP.
_REMOVED = "removed"
#: Longest bill id accepted (Merge's ids are UUIDs).
_MAX_ID = 128


@dataclass(frozen=True)
class MergeEvent:
    #: The dedup key (:func:`event_key`).
    event_key: str
    #: The Merge id of the bill the delivery names.
    bill_id: str
    #: ``Invoice.removed``: Merge says the ERP deleted it.
    removed: bool = False


def event_key(org_id: object, body: dict, data: dict) -> str:
    """A stable id for one Merge delivery to one tenant: the tenant, the
    linked account, the event, the bill and the version of it Merge sent
    (``modified_at``). With no ``modified_at`` the whole record stands in for
    it, so two different changes never share a key. The tenant is part of it so
    no tenant's deliveries, signed with its own key, can claim another's."""
    linked = body.get("linked_account")
    hook = body.get("hook")
    version = data.get("modified_at")
    if not isinstance(version, str) or not version.strip():
        version = json.dumps(data, sort_keys=True, default=str)
    parts = [
        str(org_id),
        str((linked or {}).get("id") or "") if isinstance(linked, dict) else "",
        str((hook or {}).get("event") or "") if isinstance(hook, dict) else "",
        str(data.get("id") or ""),
        str(version),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def parse_merge_delivery(body: object, org_id: object) -> MergeEvent | None:
    """The bill one verified Merge delivery names, or None.

    Only ``Invoice.*`` events: Merge sends one webhook per event type the
    tenant subscribed to (``Invoice.added`` / ``.changed`` / ``.removed``), and
    anything else (a linked-account notice, a sync completion, another model)
    names no bill. Only the bill's id is taken from the delivery: its state is
    read back from Merge (:func:`apply_merge_event`)."""
    if not isinstance(body, dict):
        return None
    hook = body.get("hook")
    event = str(hook.get("event") or "") if isinstance(hook, dict) else ""
    model, _, action = event.partition(".")
    if model != INVOICE_MODEL:
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    bill_id = data.get("id")
    if not isinstance(bill_id, str) or not bill_id or len(bill_id) > _MAX_ID:
        return None
    return MergeEvent(event_key(org_id, body, data), bill_id, removed=action == _REMOVED)


async def apply_merge_event(org, erp_config: dict, event: MergeEvent) -> str:
    """Apply one claimed event to the tenant; returns the reconciler's action.

    A bill no invoice here was pushed as (within the push's realm) is not read
    at all. Otherwise the bill is read back from Merge with the tenant's own
    credentials, as the QuickBooks and Xero webhooks read theirs (§392): a
    late redelivery of an older change cannot undo a newer one, and a body
    signed with a leaked key states nothing about the bill. A 404 counts as
    gone only for an ``Invoice.removed`` delivery; for any other it changes
    nothing. Raises on our own failure, an unreadable answer included (the
    route releases the claim and asks Merge to redeliver)."""
    from app.database import get_tenant_engine
    from app.services.erp_adapters import get_erp_adapter
    from app.services.erp_adapters.merge_dev import MergeBillNotFound
    from app.services.erp_link_realm import connection_realm
    from app.services.erp_status_sync import _bills_we_posted, apply_bill_report

    realm = connection_realm(erp_config)
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    if not await _bills_we_posted(factory, [event.bill_id], realm, by_reference=True):
        return "unmatched"
    try:
        report = await get_erp_adapter(erp_config).bill_report(event.bill_id)
    except MergeBillNotFound:
        if not event.removed:
            return "bill_not_found"
        report = ErpBillReport(
            bill_id=event.bill_id, state=ErpBillState.deleted, match_by_reference=True
        )
    async with factory() as db:
        outcome = await apply_bill_report(
            db,
            org=org,
            report=report,
            realm=realm,
            erp_type=MERGE_PROVIDER,
            event_id=event.event_key,
        )
    return outcome.action
