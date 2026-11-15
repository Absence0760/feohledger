"""The outbound webhook catalogue is one list, published and sent the same way.

`models/webhook.WEBHOOK_EVENT_TYPES` is the catalogue. Pinned here:

* every type has a `data` model and a summary (`schemas/webhook_events`), and
  nothing else does;
* the admin page's event picker (`frontend/src/lib/types/webhooks.ts`) offers
  exactly the catalogue, in the same order;
* the published OpenAPI (`/api/v1/openapi.json`) carries one `webhooks` entry
  per type, its `data` resolving to that model's component schema;
* each typed emitter sends exactly its model's fields;
* `invoice.approval_recalled` (decisions §374, §414) is emitted by
  `transition_invoice` for the recall alone — not for a resubmit that lands on
  the same `ready_for_review` — carrying the prior status and the recaller,
  and nothing a person typed.

Pure: no DB. The recall's end-to-end delivery row is pinned in
`test_invoice_recall_approval.py`.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1_openapi import build_public_openapi
from app.main import app
from app.models.invoice import InvoiceStatus
from app.models.webhook import (
    EVENT_EXCEPTION_RAISED,
    EVENT_INVOICE_APPROVAL_RECALLED,
    EVENT_INVOICE_APPROVED,
    EVENT_PAYMENT_SETTLED,
    WEBHOOK_EVENT_TYPES,
)
from app.schemas.webhook_events import WEBHOOK_EVENT_DATA_MODELS, WEBHOOK_EVENT_SUMMARIES

_FRONTEND_TYPES = pathlib.Path(__file__).resolve().parents[2] / "frontend/src/lib/types/webhooks.ts"


def test_every_catalogued_event_has_a_data_model_and_summary():
    assert list(WEBHOOK_EVENT_DATA_MODELS) == list(WEBHOOK_EVENT_TYPES)
    assert set(WEBHOOK_EVENT_SUMMARIES) == set(WEBHOOK_EVENT_TYPES)
    assert EVENT_INVOICE_APPROVAL_RECALLED in WEBHOOK_EVENT_TYPES


def test_the_admin_event_picker_offers_exactly_the_catalogue():
    source = _FRONTEND_TYPES.read_text()
    block = re.search(
        r"export const WEBHOOK_EVENT_TYPES: WebhookEventType\[\] = \[(.*?)\];", source, re.S
    )
    assert block, "WEBHOOK_EVENT_TYPES not found in webhooks.ts"
    offered = re.findall(r"'([^']+)'", block.group(1))
    assert offered == list(WEBHOOK_EVENT_TYPES)


@pytest.mark.parametrize("event_type", WEBHOOK_EVENT_TYPES)
def test_event_id_fits_the_column(event_type):
    """`webhook_deliveries.event_id` is String(64): `<type>:<uuid>` must fit."""
    assert len(f"{event_type}:{uuid.uuid4()}") <= 64


def test_the_published_openapi_carries_every_event():
    spec = build_public_openapi(app)
    assert spec["openapi"].startswith("3.1"), "`webhooks` is an OpenAPI 3.1 field"
    webhooks = spec["webhooks"]
    assert list(webhooks) == list(WEBHOOK_EVENT_TYPES)
    schemas = spec["components"]["schemas"]
    for event_type, item in webhooks.items():
        op = item["post"]
        body = op["requestBody"]["content"]["application/json"]["schema"]
        assert body["properties"]["type"]["enum"] == [event_type]
        ref = body["properties"]["data"]["$ref"]
        name = ref.rsplit("/", 1)[-1]
        assert name == WEBHOOK_EVENT_DATA_MODELS[event_type].__name__
        assert set(schemas[name]["properties"]) == set(
            WEBHOOK_EVENT_DATA_MODELS[event_type].model_fields
        )
        headers = {p["name"] for p in op["parameters"] if p["in"] == "header"}
        assert {"X-Webhook-Signature", "X-Webhook-Event-Id"} <= headers
        # A webhook is pushed by us, not called with the integrator's API key.
        assert op["security"] == []


def test_the_published_recall_payload_names_the_prior_status_and_no_person():
    spec = build_public_openapi(app)
    props = spec["components"]["schemas"]["WebhookInvoiceApprovalRecalledData"]["properties"]
    assert {"recalled_from", "invoice_id", "status"} <= set(props)
    # No person reference, like invoice.approved (decisions §421); no reason.
    assert not {"reason", "recalled_by_user_id"} & set(props)
    assert not any("user" in p for p in props)
    assert props["amount"]["anyOf"][0]["type"] == "string"


def test_the_docs_page_lists_the_webhooks():
    from app.api.v1_openapi import render_docs_html

    page = render_docs_html(build_public_openapi(app))
    assert "<h2>Webhooks</h2>" in page
    for event_type in WEBHOOK_EVENT_TYPES:
        assert f"<code>{event_type}</code>" in page


# ── What each emitter sends ─────────────────────────────────────────────────


def _invoice(status=InvoiceStatus.ready_for_review):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        invoice_number="INV-414",
        vendor_name="Acme",
        amount=Decimal("1234.50"),
        currency="EUR",
        status=status,
    )


@pytest.fixture
def captured(monkeypatch):
    from app.services.webhooks import dispatch

    calls: list[dict] = []

    async def _fake_emit_event(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(dispatch, "emit_event", _fake_emit_event)
    return calls


async def test_each_emitter_sends_exactly_its_models_fields(captured):
    from app.services.webhooks import dispatch

    inv = _invoice(InvoiceStatus.approved)
    await dispatch.emit_invoice_approved(inv, occurrence_id=uuid.uuid4())
    await dispatch.emit_invoice_approval_recalled(inv, recalled_from="approved")
    await dispatch.emit_payment_settled(inv, occurrence_id=uuid.uuid4())
    await dispatch.emit_exception_raised(
        organization_id=inv.organization_id,
        exception_id=uuid.uuid4(),
        exception_type="duplicate",
        invoice=inv,
    )
    sent = {c["event_type"]: c["data"] for c in captured}
    assert set(sent) == set(WEBHOOK_EVENT_TYPES)
    for event_type, data in sent.items():
        assert set(data) == set(WEBHOOK_EVENT_DATA_MODELS[event_type].model_fields)
    # Money stays an exact string on every invoice event.
    for event_type in (EVENT_INVOICE_APPROVED, EVENT_PAYMENT_SETTLED, EVENT_EXCEPTION_RAISED):
        assert sent[event_type]["amount"] == "1234.50"


async def test_the_recall_payload(captured):
    from app.services.webhooks import dispatch

    inv = _invoice()
    occurrence = uuid.uuid4()
    await dispatch.emit_invoice_approval_recalled(
        inv, recalled_from="failed", occurrence_id=occurrence
    )
    (call,) = captured
    assert call["event_type"] == EVENT_INVOICE_APPROVAL_RECALLED
    assert call["event_key"] == str(occurrence)
    assert call["data"] == {
        "invoice_id": str(inv.id),
        "invoice_number": "INV-414",
        "vendor_name": "Acme",
        "amount": "1234.50",
        "currency": "EUR",
        "status": "ready_for_review",
        "recalled_from": "failed",
    }


# ── transition_invoice routes the recall, and only the recall ───────────────


@pytest.fixture
def engine_emits(monkeypatch):
    from app.services import webhooks as webhooks_pkg
    from app.services import workflow_engine as we

    calls: list[tuple[str, dict]] = []

    def _recorder(name):
        async def _fake(invoice, **kwargs):
            calls.append((name, kwargs))

        return _fake

    monkeypatch.setattr(webhooks_pkg, "emit_invoice_approved", _recorder("approved"))
    monkeypatch.setattr(webhooks_pkg, "emit_payment_settled", _recorder("settled"))
    monkeypatch.setattr(webhooks_pkg, "emit_invoice_approval_recalled", _recorder("recalled"))
    monkeypatch.setattr(we, "dispatch_audit", AsyncMock())
    monkeypatch.setattr(we, "_maybe_notify_transition", AsyncMock())
    return calls


@pytest.mark.parametrize("from_status", [InvoiceStatus.approved, InvoiceStatus.failed])
async def test_the_recall_transition_emits_the_recall_event(engine_emits, from_status):
    from app.services import workflow_engine as we

    inv = _invoice(from_status)
    inv.approved_by, inv.approval_date = "Original Approver", None
    actor = uuid.uuid4()
    await we.transition_invoice(
        MagicMock(),
        inv,
        InvoiceStatus.ready_for_review,
        actor_id=actor,
        action_name=we.RECALL_ACTION,
        details={"reason": "Wrong payee"},
    )
    ((name, kwargs),) = engine_emits
    assert name == "recalled"
    assert kwargs["recalled_from"] == from_status.value
    assert "recalled_by_user_id" not in kwargs
    assert kwargs["occurrence_id"] is not None
    assert "reason" not in kwargs


async def test_a_resubmit_to_review_emits_nothing(engine_emits):
    """`ready_for_review` is also where a resubmit lands; only the recall is
    an event."""
    from app.services import workflow_engine as we

    inv = _invoice(InvoiceStatus.rejected)
    await we.transition_invoice(
        MagicMock(),
        inv,
        InvoiceStatus.ready_for_review,
        actor_id=uuid.uuid4(),
        action_name="invoice.resubmitted",
    )
    assert engine_emits == []


async def test_an_approval_still_emits_invoice_approved(engine_emits):
    from app.services import workflow_engine as we

    inv = _invoice(InvoiceStatus.ready_for_review)
    await we.transition_invoice(
        MagicMock(), inv, InvoiceStatus.approved, action_name="invoice.approved"
    )
    assert [name for name, _ in engine_emits] == ["approved"]
