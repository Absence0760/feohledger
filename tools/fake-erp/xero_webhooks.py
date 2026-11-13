"""Xero webhook deliveries, built and signed the way Xero sends them.

Stdlib only, so the backend's tests load this file in-process
(`backend/tests/test_xero_webhook.py`) and prove the route accepts exactly
what the fake signs. `app.py`'s `POST /xero/__webhook` returns one for an e2e
spec to POST to `/api/erp/webhook/xero` (decisions §392).

Xero's shape: `{"events": [...], "firstEventSequence", "lastEventSequence",
"entropy"}`, each event naming its resource and organisation and nothing of
the resource's state; the intent-to-receive check is the same envelope with no
events. `x-xero-signature` is the base64 HMAC-SHA256 of the raw body under
the app's webhook key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime

#: backend/.env.development's FEOH_ERP_XERO_WEBHOOK_KEY.
DEV_WEBHOOK_KEY = "dev-xero-webhook-key"
API_BASE = "https://api.xero.com/api.xro/2.0"


def invoice_event(
    tenant_id: str, invoice_id: str, *, event_type: str = "UPDATE", at: datetime | None = None
) -> dict:
    """One INVOICE event for `invoice_id` in organisation `tenant_id`."""
    moment = (at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    return {
        "resourceUrl": f"{API_BASE}/Invoices/{invoice_id}",
        "resourceId": invoice_id,
        "eventDateUtc": moment,
        "eventType": event_type,
        "eventCategory": "INVOICE",
        "tenantId": tenant_id,
        "tenantType": "ORGANISATION",
    }


def signed_delivery(events: list[dict], *, key: str = DEV_WEBHOOK_KEY) -> dict[str, str]:
    """`{"body": <raw JSON>, "signature": <x-xero-signature>}`. Sent as the
    body string verbatim: re-serialising it would change the bytes signed."""
    body = json.dumps(
        {
            "events": events,
            "firstEventSequence": 1 if events else 0,
            "lastEventSequence": len(events),
            "entropy": secrets.token_hex(10).upper(),
        }
    )
    digest = hmac.new(key.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return {"body": body, "signature": base64.b64encode(digest).decode("ascii")}
