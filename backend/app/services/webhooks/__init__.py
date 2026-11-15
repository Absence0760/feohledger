"""Outbound-webhook dispatch package.

Public surface:

* ``emit_event`` — durably enqueue a ``WebhookDelivery`` per matching active
  subscription for a domain event (best-effort; never raises into the caller).
* ``process_delivery`` / ``deliver_due`` — sign + POST a delivery with bounded
  retries + exponential backoff, dead-lettering after exhaustion.
* ``run_webhook_delivery_loop`` — the background retry sweep (gated behind
  ``FEOH_WEBHOOKS_ENABLED``).
* ``generate_signing_secret`` / ``sign_payload`` — the per-subscription secret
  + the HMAC-SHA256 signature reused from ``webhook_security``.
"""

from app.services.webhooks.delivery import deliver_due, process_delivery, run_webhook_delivery_loop
from app.services.webhooks.dispatch import (
    emit_event,
    emit_exception_raised,
    emit_invoice_approval_recalled,
    emit_invoice_approved,
    emit_payment_settled,
)
from app.services.webhooks.signing import (
    SECRET_BRAND,
    SECRET_PREFIX_LEN,
    generate_signing_secret,
    sign_payload,
)

__all__ = [
    "emit_event",
    "emit_invoice_approved",
    "emit_invoice_approval_recalled",
    "emit_payment_settled",
    "emit_exception_raised",
    "process_delivery",
    "deliver_due",
    "run_webhook_delivery_loop",
    "generate_signing_secret",
    "sign_payload",
    "SECRET_BRAND",
    "SECRET_PREFIX_LEN",
]
