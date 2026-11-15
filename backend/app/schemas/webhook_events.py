"""The ``data`` object of every outbound webhook event — one model per event type.

These models are the single definition of each event's payload. The emitters in
``services/webhooks/dispatch`` build ``data`` THROUGH them (``model_dump()``), and
``api/v1_openapi`` publishes them under the OpenAPI 3.1 ``webhooks`` section, so
what an integrator reads in the published contract is, by construction, what
their receiver is sent. ``WEBHOOK_EVENT_DATA_MODELS`` must name exactly the
types in ``models/webhook.WEBHOOK_EVENT_TYPES``;
``tests/test_webhook_event_catalogue.py`` holds the two together.

Every payload is PII-free: identifiers, statuses, the invoice's public-facing
fields (number, vendor *name*, amount as an exact string, currency). No tax id,
bank detail, address, person's name or email, and no free text a person typed
(a recall's reason stays in the audit row).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.webhook import (
    EVENT_EXCEPTION_RAISED,
    EVENT_INVOICE_APPROVAL_RECALLED,
    EVENT_INVOICE_APPROVED,
    EVENT_PAYMENT_SETTLED,
)

_AMOUNT = "Invoice amount as an exact decimal string (never a float)."


class WebhookInvoiceData(BaseModel):
    """``invoice.approved`` / ``payment.settled``: the invoice that reached the
    status, as it stood at the transition."""

    invoice_id: str = Field(description="The invoice's id.")
    invoice_number: str | None = Field(None, description="The supplier's invoice number.")
    vendor_name: str | None = Field(None, description="The vendor's name.")
    amount: str | None = Field(None, description=_AMOUNT)
    currency: str = Field(description="ISO 4217 currency code of `amount`.")
    status: str | None = Field(None, description="The invoice's status after the transition.")


class WebhookInvoiceApprovalRecalledData(WebhookInvoiceData):
    """``invoice.approval_recalled``: an approval this receiver may already have
    acted on (from ``invoice.approved``) was withdrawn, and the invoice is back
    in review (decisions §374). It is not payable until it is approved again,
    which emits a fresh ``invoice.approved``."""

    recalled_from: str = Field(
        description=(
            "The status the approval was recalled from: `approved`, or `failed` "
            "(an approved invoice whose ERP push failed)."
        )
    )


class WebhookExceptionData(BaseModel):
    """``exception.raised``: an AP exception was opened."""

    exception_id: str = Field(description="The exception's id.")
    exception_type: str = Field(description="The exception classification, e.g. `duplicate`.")
    severity: str | None = Field(None, description="The exception's severity.")
    status: str | None = Field(None, description="The exception's status, normally `open`.")
    invoice_id: str | None = Field(None, description="The linked invoice's id, if any.")
    invoice_number: str | None = Field(None, description="The linked invoice's number.")
    vendor_name: str | None = Field(None, description="The linked invoice's vendor name.")
    amount: str | None = Field(None, description=_AMOUNT)
    currency: str = Field(description="ISO 4217 currency code of `amount`.")
    link: str = Field(description="App-relative deep link to the invoice or the exception queue.")


#: Event type → the model of its ``data`` object. Keys are exactly
#: ``WEBHOOK_EVENT_TYPES``.
WEBHOOK_EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    EVENT_INVOICE_APPROVED: WebhookInvoiceData,
    EVENT_INVOICE_APPROVAL_RECALLED: WebhookInvoiceApprovalRecalledData,
    EVENT_PAYMENT_SETTLED: WebhookInvoiceData,
    EVENT_EXCEPTION_RAISED: WebhookExceptionData,
}

#: One-line summary per event type, published beside its payload.
WEBHOOK_EVENT_SUMMARIES: dict[str, str] = {
    EVENT_INVOICE_APPROVED: "An invoice was approved.",
    EVENT_INVOICE_APPROVAL_RECALLED: (
        "An invoice's approval was recalled; the invoice is back in review and "
        "not payable until approved again."
    ),
    EVENT_PAYMENT_SETTLED: "An invoice was paid.",
    EVENT_EXCEPTION_RAISED: "An AP exception was opened.",
}


def webhook_envelope_schema(event_type: str, data_ref: str) -> dict[str, Any]:
    """JSON Schema of the signed envelope every delivery's body carries
    (``services/webhooks/dispatch._emit``), with ``data`` pointing at
    ``data_ref``."""
    return {
        "type": "object",
        "required": ["id", "type", "created_at", "organization_id", "data"],
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "Event id, `<type>:<occurrence-id>`; also sent as "
                    "`X-Webhook-Event-Id`. Dedupe on it."
                ),
            },
            "type": {"type": "string", "enum": [event_type]},
            "created_at": {"type": "string", "format": "date-time"},
            "organization_id": {"type": "string"},
            "data": {"$ref": data_ref},
        },
    }
