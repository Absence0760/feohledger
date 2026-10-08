"""`exception.raised` outbound-webhook event — emitted from the shared
`services/exception_service.create_exception` chokepoint.

Covers the deferred event source now wired:
  * creating an Exception through the chokepoint enqueues ONE `exception.raised`
    delivery to a subscribed active subscription;
  * the payload is PII-free and money serialises as an exact string;
  * a webhook-emit failure NEVER breaks exception creation (best-effort);
  * FEOH_WEBHOOKS_ENABLED off → no delivery;
  * the same exception id dedupes to a single delivery (re-fire / replay);
  * an invoice-less exception (Positive Pay never-issued cheque) still emits
    with identifiers only.

Uses the real-Postgres harness (control-plane subscriptions + a tenant invoice).
The fire-and-forget immediate delivery attempt is suppressed so the tests read
only the enqueue/dedupe behaviour (mirrors test_outbound_webhooks.py).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.invoice import Invoice
from app.models.webhook import (
    EVENT_EXCEPTION_RAISED,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.webhooks.signing import generate_signing_secret

# Outbound webhooks are plan-gated (decisions §258) — both the subscription CRUD
# and dispatch, which queues nothing for an org without the feature — and the
# realdb harness orgs hold no subscription (= free), so bind both to Scale.
pytestmark = pytest.mark.plan("scale")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_sub(org_id, *, events=(EVENT_EXCEPTION_RAISED,), active=True):
    secret, prefix = generate_signing_secret()
    return WebhookSubscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="exc-hook",
        target_url="https://example.test/hook",
        event_types=list(events),
        signing_secret=secret,
        secret_prefix=prefix,
        active=active,
    )


async def _persist_sub(control_mk, *subs):
    async with control_mk() as s:
        for sub in subs:
            s.add(sub)
        await s.commit()


async def _cleanup(control_mk, *sub_ids):
    async with control_mk() as s:
        for sid in sub_ids:
            await s.execute(delete(WebhookDelivery).where(WebhookDelivery.subscription_id == sid))
            await s.execute(delete(WebhookSubscription).where(WebhookSubscription.id == sid))
        await s.commit()


async def _deliveries(control_mk, org_id, *, event_type=EVENT_EXCEPTION_RAISED):
    async with control_mk() as s:
        return (
            (
                await s.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.organization_id == org_id,
                        WebhookDelivery.event_type == event_type,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _make_invoice(mk, org_id):
    # entity_id left NULL (nullable FK) so the test doesn't depend on the seed's
    # default-Entity row — the webhook payload reads invoice fields, not entity.
    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=org_id,
        invoice_number="INV-WEBHOOK-001",
        vendor_name="Globex Corporation",
        amount=Decimal("1234.56"),
        currency="EUR",
        status="new",
    )
    async with mk() as s:
        s.add(inv)
        await s.commit()
    return inv


@pytest_asyncio.fixture(autouse=True)
async def _rebind_control_engine_to_test_loop():
    """`emit_event` opens the GLOBAL `control_session_factory` (production-correct
    — webhook subscriptions live in the control plane). That engine pools
    connections bound to whatever loop first used it, so a later test on a fresh
    loop trips asyncpg's "attached to a different loop". Disposing it before each
    test forces a clean rebind to the current loop — same flake-class the realdb
    docs call out (cross-test event-loop binding); no production behaviour
    changes. Dispose again after so we don't strand this loop's connections."""
    from app.database import control_engine

    await control_engine.dispose()
    yield
    await control_engine.dispose()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_exception_emits_exception_raised_with_clean_payload(realdb, monkeypatch):
    from app.config import settings
    from app.services.exception_service import create_exception
    from app.services.webhooks import dispatch as dispatch_mod

    org_id = realdb.info("a").org_id
    control_mk = realdb.control_sessionmaker()
    mk = realdb.sessionmaker("a")
    inv = await _make_invoice(mk, org_id)

    sub = _make_sub(org_id)
    await _persist_sub(control_mk, sub)

    monkeypatch.setattr(settings, "webhooks_enabled", True)
    monkeypatch.setattr(dispatch_mod, "_spawn_immediate_attempt", lambda did: None)

    try:
        async with mk() as s:
            row = await s.get(Invoice, inv.id)
            await create_exception(
                s,
                exception_type="duplicate",
                severity="warning",
                description="Looks like a duplicate of INV-WEBHOOK-001",
                organization_id=org_id,
                invoice=row,
            )
            await s.commit()

        rows = await _deliveries(control_mk, org_id)
        assert len(rows) == 1, "one exception.raised delivery to the subscribed sub"
        dlv = rows[0]
        assert dlv.subscription_id == sub.id
        data = dlv.payload["data"]
        # Identifiers + classification + invoice public fields, money as string.
        assert data["exception_type"] == "duplicate"
        assert data["severity"] == "warning"
        assert data["status"] == "open"
        assert data["invoice_id"] == str(inv.id)
        assert data["invoice_number"] == "INV-WEBHOOK-001"
        assert data["vendor_name"] == "Globex Corporation"
        assert data["amount"] == "1234.56"
        assert isinstance(data["amount"], str)
        assert data["currency"] == "EUR"
        assert data["link"] == f"/invoices/{inv.id}"
        # event id is derived from the exception id → stable for dedupe.
        assert dlv.event_id.startswith(f"{EVENT_EXCEPTION_RAISED}:")

        # PII-free: nothing in the serialised body resembles bank/tax data.
        body = json.dumps(dlv.payload)
        assert "account_number" not in body
        assert "tax_id" not in body
        assert "routing" not in body
    finally:
        await _cleanup(control_mk, sub.id)
        async with mk() as s:
            from app.models.exception import Exception as APException

            await s.execute(delete(APException).where(APException.invoice_id == inv.id))
            await s.execute(delete(Invoice).where(Invoice.id == inv.id))
            await s.commit()


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_exception_creation(realdb, monkeypatch):
    """A webhook-emit blowup must not stop the Exception row from being written."""
    from app.config import settings
    from app.services import exception_service
    from app.services.exception_service import create_exception

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    inv = await _make_invoice(mk, org_id)

    monkeypatch.setattr(settings, "webhooks_enabled", True)

    async def boom(**kwargs):
        raise RuntimeError("webhook subsystem exploded")

    # Patch the symbol the helper imports at call time.
    import app.services.webhooks as webhooks_pkg

    monkeypatch.setattr(webhooks_pkg, "emit_exception_raised", boom)

    from app.models.exception import Exception as APException

    try:
        async with mk() as s:
            row = await s.get(Invoice, inv.id)
            exc = await create_exception(
                s,
                exception_type="fraud_flag",
                severity="error",
                description="suspicious",
                organization_id=org_id,
                invoice=row,
            )
            await s.commit()
            created_id = exc.id

        # The exception row persisted despite the emit failure.
        async with mk() as s:
            got = await s.get(APException, created_id)
            assert got is not None
            assert got.exception_type == "fraud_flag"
    finally:
        async with mk() as s:
            await s.execute(delete(APException).where(APException.invoice_id == inv.id))
            await s.execute(delete(Invoice).where(Invoice.id == inv.id))
            await s.commit()
        # touch to keep the symbol referenced for ruff
        assert exception_service is not None


@pytest.mark.asyncio
async def test_no_emit_when_webhooks_disabled(realdb, monkeypatch):
    from app.config import settings
    from app.services.exception_service import create_exception

    org_id = realdb.info("a").org_id
    control_mk = realdb.control_sessionmaker()
    mk = realdb.sessionmaker("a")
    inv = await _make_invoice(mk, org_id)

    sub = _make_sub(org_id)
    await _persist_sub(control_mk, sub)

    monkeypatch.setattr(settings, "webhooks_enabled", False)

    from app.models.exception import Exception as APException

    try:
        async with mk() as s:
            row = await s.get(Invoice, inv.id)
            await create_exception(
                s,
                exception_type="po_mismatch",
                severity="warning",
                description="amount over PO",
                organization_id=org_id,
                invoice=row,
            )
            await s.commit()

        assert await _deliveries(control_mk, org_id) == []
    finally:
        await _cleanup(control_mk, sub.id)
        async with mk() as s:
            await s.execute(delete(APException).where(APException.invoice_id == inv.id))
            await s.execute(delete(Invoice).where(Invoice.id == inv.id))
            await s.commit()


@pytest.mark.asyncio
async def test_same_exception_dedupes_to_one_delivery(realdb, monkeypatch):
    """Re-emitting for the same exception id yields a single delivery."""
    from app.config import settings
    from app.services.webhooks import dispatch as dispatch_mod
    from app.services.webhooks import emit_exception_raised

    org_id = realdb.info("a").org_id
    control_mk = realdb.control_sessionmaker()

    sub = _make_sub(org_id)
    await _persist_sub(control_mk, sub)

    monkeypatch.setattr(settings, "webhooks_enabled", True)
    monkeypatch.setattr(dispatch_mod, "_spawn_immediate_attempt", lambda did: None)

    exc_id = uuid.uuid4()
    try:
        for _ in range(2):
            await emit_exception_raised(
                organization_id=org_id,
                exception_id=exc_id,
                exception_type="amount_exceeded",
                severity="warning",
                status="open",
                invoice=None,
                invoice_id=None,
            )
        rows = await _deliveries(control_mk, org_id)
        assert len(rows) == 1
        assert rows[0].event_id == f"{EVENT_EXCEPTION_RAISED}:{exc_id}"
    finally:
        await _cleanup(control_mk, sub.id)


@pytest.mark.asyncio
async def test_invoiceless_exception_emits_identifiers_only(realdb, monkeypatch):
    """A Positive-Pay never-issued cheque has no invoice → payload carries no
    number / vendor / amount, link points at the exceptions queue."""
    from app.config import settings
    from app.services.exception_service import create_exception
    from app.services.webhooks import dispatch as dispatch_mod

    org_id = realdb.info("a").org_id
    control_mk = realdb.control_sessionmaker()
    mk = realdb.sessionmaker("a")
    sub = _make_sub(org_id)
    await _persist_sub(control_mk, sub)

    monkeypatch.setattr(settings, "webhooks_enabled", True)
    monkeypatch.setattr(dispatch_mod, "_spawn_immediate_attempt", lambda did: None)

    from app.models.exception import Exception as APException

    try:
        async with mk() as s:
            await create_exception(
                s,
                exception_type="fraud_flag",
                severity="error",
                description="Positive Pay return: check 5001 not on issued file",
                organization_id=org_id,
                invoice_id=None,
            )
            await s.commit()

        rows = await _deliveries(control_mk, org_id)
        assert len(rows) == 1
        data = rows[0].payload["data"]
        assert data["invoice_id"] is None
        assert data["invoice_number"] is None
        assert data["vendor_name"] is None
        assert data["amount"] is None
        assert data["link"] == "/exceptions"
    finally:
        await _cleanup(control_mk, sub.id)
        async with mk() as s:
            await s.execute(
                delete(APException).where(
                    APException.organization_id == org_id,
                    APException.invoice_id.is_(None),
                    APException.description.like("Positive Pay return: check 5001%"),
                )
            )
            await s.commit()
