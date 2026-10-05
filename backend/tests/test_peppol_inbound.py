"""Tests for PEPPOL AS4 inbound receive — webhook route + receive service.

Covers (per the slice test plan):
  - signed happy path → 204, exactly one Invoice(status=new) + one inbound
    PeppolTransmission(status='delivered'), money as Decimal from the UBL;
  - redelivery (same MessageId) → 204, still exactly one invoice (fast-path
    dedupe SELECT);
  - concurrent-redelivery race → the second receive hits the uq_peppol_message_id
    guarantee, rolls back, creates NO second invoice, returns duplicate=True;
  - bad / missing signature → 204, no invoice (signature gate, no enumeration);
  - unknown tenant → 204 (no slug enumeration);
  - malformed / non-UBL payload → 204, no invoice (soft reject);
  - disabled master switch → 204, no invoice;
  - no supplier PII (sender_value / tax id) in logs or the response body;
  - boot guard refuses without the signing secret when not in debug;
  - mock adapter parse_inbound returns a populated message / None.

Auth-gating itself is covered by test_rbac.py (the route IS in ALTERNATE_AUTH).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.peppol_transmission import PeppolTransmission
from app.models.workflow import AuditLog
from app.services.e_invoice import generate_ubl
from app.services.e_invoice.model import (
    EInvoiceDocument,
    EInvoiceFormat,
    EInvoiceLine,
    EInvoiceParty,
)

_DEV_SECRET = "dev-peppol-inbound-secret"
_SUPPLIER_TAX_ID = "DE999888777"  # PII — must never appear in a log or body
_SENDER_SCHEME = "9930"
_SENDER_VALUE = "DE-SUPPLIER-PII-VALUE"


def _ubl_bytes(*, invoice_number="SUP-INV-001", currency="EUR") -> bytes:
    doc = EInvoiceDocument(
        source_format=EInvoiceFormat.UBL,
        invoice_number=invoice_number,
        issue_date=date(2026, 1, 15),
        currency=currency,
        seller=EInvoiceParty(name="Supplier GmbH", tax_id=_SUPPLIER_TAX_ID, country_code="DE"),
        buyer=EInvoiceParty(name="Acme Buyer", country_code="DE"),
        line_extension_amount=Decimal("100.00"),
        tax_exclusive_amount=Decimal("100.00"),
        tax_inclusive_amount=Decimal("119.00"),
        tax_total=Decimal("19.00"),
        payable_amount=Decimal("119.00"),
        lines=[
            EInvoiceLine(
                line_id="1",
                description="Widget",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                line_total=Decimal("100.00"),
            )
        ],
    )
    return generate_ubl(doc)


def _envelope(*, message_id: str, payload: bytes) -> bytes:
    """Dev JSON envelope the mock adapter parses."""
    return json.dumps(
        {
            "message_id": message_id,
            "sender_scheme": _SENDER_SCHEME,
            "sender_value": _SENDER_VALUE,
            "doc_type_id": "urn:doc-type",
            "process_id": "urn:process",
            "payload_base64": base64.b64encode(payload).decode("ascii"),
        }
    ).encode("utf-8")


def _sign(body: bytes, secret: str = _DEV_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _enable_inbound(monkeypatch):
    """Turn inbound on with a known signing secret for every test (each test
    that needs the switch off overrides it), and stub dispatch_extraction so the
    in-process worker thread doesn't mutate the just-created invoice mid-assert
    (extraction itself is exercised by the einvoice / extraction test suites)."""
    from unittest.mock import AsyncMock

    from app.config import settings

    monkeypatch.setattr(settings, "peppol_inbound_enabled", True)
    monkeypatch.setattr(settings, "peppol_inbound_signing_secret", _DEV_SECRET)
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", AsyncMock())


async def _count(mk, model, **where) -> int:
    async with mk() as s:
        q = select(func.count()).select_from(model)
        for col, val in where.items():
            q = q.where(getattr(model, col) == val)
        return (await s.execute(q)).scalar_one()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_inbound_creates_invoice_and_transmission(realdb):
    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    message_id = f"as4-{uuid.uuid4().hex}"
    body = _envelope(message_id=message_id, payload=_ubl_bytes())

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=body,
            headers={"X-Peppol-Signature": _sign(body)},
        )

    assert resp.status_code == 204

    async with mk() as s:
        invoices = (await s.execute(select(Invoice))).scalars().all()
        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.status == InvoiceStatus.new
        assert inv.invoice_number == "SUP-INV-001"
        assert inv.vendor_name == "Supplier GmbH"
        assert inv.amount == Decimal("119.00")
        assert isinstance(inv.amount, Decimal)
        assert inv.currency == "EUR"
        assert inv.file_key and inv.file_key.endswith("peppol-inbound.xml")
        # The authorising proxy, never the private bucket's address (decisions §226).
        assert inv.file_url == f"/api/invoices/file/{inv.file_key}"

        txns = (await s.execute(select(PeppolTransmission))).scalars().all()
        assert len(txns) == 1
        t = txns[0]
        assert t.direction == "inbound"
        assert t.status == "delivered"
        assert t.message_id == message_id
        assert t.invoice_id == inv.id
        assert t.amount == Decimal("119.00")

        # The SOX-regulated append-only audit row proving inbound receipt.
        audit_rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "invoice.peppol_received",
                        AuditLog.entity_id == inv.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1
        row = audit_rows[0]
        assert row.actor_id == uuid.UUID("00000000-0000-0000-0000-000000000000")
        # PII-free details: scheme present, the supplier PII value absent.
        assert row.details["sender_scheme"] == _SENDER_SCHEME
        assert _SENDER_VALUE not in json.dumps(row.details)


# ---------------------------------------------------------------------------
# Dedupe — sequential redelivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redelivery_is_deduped_no_second_invoice(realdb):
    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    message_id = f"as4-{uuid.uuid4().hex}"
    body = _envelope(message_id=message_id, payload=_ubl_bytes())
    sig = _sign(body)

    async with realdb.client(key="a", role=None) as c:
        first = await c.post(
            f"/api/peppol/inbound/{slug}", content=body, headers={"X-Peppol-Signature": sig}
        )
        second = await c.post(
            f"/api/peppol/inbound/{slug}", content=body, headers={"X-Peppol-Signature": sig}
        )

    assert first.status_code == 204
    assert second.status_code == 204
    assert await _count(mk, Invoice) == 1
    assert await _count(mk, PeppolTransmission) == 1


# ---------------------------------------------------------------------------
# Dedupe — concurrent-redelivery race (DB unique index is the guarantee)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_redelivery_race_one_invoice(realdb):
    """A committed transmission with the target message_id already exists (the
    winner). A second receive of the same MessageId must NOT create a second
    invoice — its transmission INSERT (or the fast-path SELECT) hits the
    uq_peppol_message_id guarantee, rolls back, and returns duplicate=True."""
    from app.database import control_session_factory
    from app.services.peppol_receive import InboundPeppolMessage, receive_peppol_message

    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    org_id = realdb.info("a").org_id
    message_id = f"as4-race-{uuid.uuid4().hex}"

    # Pre-insert the "winner" transmission (committed) on a stand-in invoice.
    winner_invoice_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=winner_invoice_id,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
                invoice_number="WINNER",
                vendor_name="Supplier GmbH",
                amount=Decimal("119.00"),
                currency="EUR",
                status=InvoiceStatus.new,
            )
        )
        s.add(
            PeppolTransmission(
                invoice_id=winner_invoice_id,
                direction="inbound",
                participant_scheme=_SENDER_SCHEME,
                participant_value=_SENDER_VALUE,
                doc_type_id="urn:doc-type",
                process_id="urn:process",
                business_message_id=message_id,
                message_id=message_id,
                status="delivered",
                provider="mock",
                amount=Decimal("119.00"),
                currency="EUR",
                organization_id=org_id,
            )
        )
        await s.commit()

    message = InboundPeppolMessage(
        message_id=message_id,
        sender_scheme=_SENDER_SCHEME,
        sender_value=_SENDER_VALUE,
        doc_type_id="urn:doc-type",
        process_id="urn:process",
        payload=_ubl_bytes(),
    )
    async with control_session_factory() as ctrl_db:
        result = await receive_peppol_message(ctrl_db, tenant_slug=slug, message=message)

    assert result.duplicate is True
    assert result.invoice_id is None
    # Still exactly one invoice (the pre-seeded winner) and one transmission.
    assert await _count(mk, Invoice) == 1
    assert await _count(mk, PeppolTransmission) == 1


# ---------------------------------------------------------------------------
# Dedupe — true IntegrityError branch (both receives pass the fast-path SELECT)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_receive_integrityerror_one_invoice(realdb, monkeypatch):
    """Drive the AUTHORITATIVE race defence, not just the fast-path SELECT: make
    both receives pass the advisory dedupe SELECT (force it to return None), so
    the loser only stops at the transmission flush() — where uq_peppol_message_id
    raises IntegrityError, rolls back the whole tenant txn (invoice included), and
    returns duplicate=True. Exactly one Invoice + one transmission must survive."""
    from app.services import peppol_receive as pr
    from app.services.peppol_receive import InboundPeppolMessage, receive_peppol_message

    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    slug = realdb.info("a").slug
    message_id = f"as4-ie-{uuid.uuid4().hex}"

    # Neuter the fast-path SELECT so BOTH receives reach the transmission flush.
    # Patch the module's `select` so the dedupe pre-check resolves to no row even
    # after the winner committed — exactly the concurrent window the index guards.
    real_select = pr.select
    seen_dedupe = {"n": 0}

    def _fake_select(*args, **kwargs):
        # The dedupe pre-check selects PeppolTransmission.id; force it to a query
        # that returns nothing the first two calls (the two racing receives).
        if args and args[0] is PeppolTransmission.id and seen_dedupe["n"] < 2:
            seen_dedupe["n"] += 1
            return real_select(PeppolTransmission.id).where(
                PeppolTransmission.id == uuid.UUID("00000000-0000-0000-0000-000000000000")
            )
        return real_select(*args, **kwargs)

    monkeypatch.setattr(pr, "select", _fake_select)

    def _msg() -> InboundPeppolMessage:
        return InboundPeppolMessage(
            message_id=message_id,
            sender_scheme=_SENDER_SCHEME,
            sender_value=_SENDER_VALUE,
            doc_type_id="urn:doc-type",
            process_id="urn:process",
            payload=_ubl_bytes(),
        )

    # First receive commits the winner (its flush claims the slot).
    async with ctrl_mk() as ctrl1:
        r1 = await receive_peppol_message(ctrl1, tenant_slug=slug, message=_msg())
    # Second receive passes the (neutered) SELECT, then its flush hits the index.
    async with ctrl_mk() as ctrl2:
        r2 = await receive_peppol_message(ctrl2, tenant_slug=slug, message=_msg())

    assert r1.accepted is True
    assert r2.accepted is False
    assert r2.duplicate is True
    assert r2.invoice_id is None
    # The DB index — not the app pre-check — kept it to one invoice + one txn.
    assert await _count(mk, Invoice) == 1
    assert await _count(mk, PeppolTransmission) == 1


# ---------------------------------------------------------------------------
# Signature gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_signature_returns_204_no_invoice(realdb):
    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    body = _envelope(message_id=f"as4-{uuid.uuid4().hex}", payload=_ubl_bytes())

    async with realdb.client(key="a", role=None) as c:
        bad = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=body,
            headers={"X-Peppol-Signature": "deadbeef"},
        )
        missing = await c.post(f"/api/peppol/inbound/{slug}", content=body)

    assert bad.status_code == 204
    assert missing.status_code == 204
    assert await _count(mk, Invoice) == 0


# ---------------------------------------------------------------------------
# Unknown tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tenant_returns_204(realdb):
    body = _envelope(message_id=f"as4-{uuid.uuid4().hex}", payload=_ubl_bytes())
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            "/api/peppol/inbound/does-not-exist",
            content=body,
            headers={"X-Peppol-Signature": _sign(body)},
        )
    assert resp.status_code == 204
    assert "does-not-exist" not in resp.text


# ---------------------------------------------------------------------------
# Malformed document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_document_returns_204_no_invoice(realdb):
    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    body = _envelope(
        message_id=f"as4-{uuid.uuid4().hex}", payload=b"<not-an-invoice>garbage</not-an-invoice>"
    )

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=body,
            headers={"X-Peppol-Signature": _sign(body)},
        )

    assert resp.status_code == 204
    assert await _count(mk, Invoice) == 0
    assert await _count(mk, PeppolTransmission) == 0


# ---------------------------------------------------------------------------
# Route-level parse_inbound → None guard (unparseable envelope)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unparseable_envelope_returns_204_no_invoice(realdb):
    """A signed body the adapter cannot parse into an InboundPeppolMessage (no
    JSON envelope → no MessageId → can't dedupe) hits the route's parse_inbound
    None guard: 204, no invoice, no transmission — exercised end-to-end."""
    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    # Raw UBL with no dev JSON envelope wrapper → mock adapter returns None.
    body = b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=body,
            headers={"X-Peppol-Signature": _sign(body)},
        )

    assert resp.status_code == 204
    assert await _count(mk, Invoice) == 0
    assert await _count(mk, PeppolTransmission) == 0


# ---------------------------------------------------------------------------
# Body size cap (unbounded-payload DoS guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_body_returns_204_no_invoice(realdb, monkeypatch):
    """A signed-but-oversized POST is rejected (204) BEFORE parsing — the
    memory-exhaustion guard. No invoice, no transmission."""
    from app.config import settings

    monkeypatch.setattr(settings, "peppol_inbound_max_bytes", 1024)
    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    # A valid signed envelope, but larger than the 1 KB test cap.
    body = _envelope(
        message_id=f"as4-{uuid.uuid4().hex}",
        payload=_ubl_bytes(invoice_number="X" * 4096),
    )
    assert len(body) > 1024

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=body,
            headers={"X-Peppol-Signature": _sign(body)},
        )

    assert resp.status_code == 204
    assert await _count(mk, Invoice) == 0
    assert await _count(mk, PeppolTransmission) == 0


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_master_switch_204(realdb, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "peppol_inbound_enabled", False)
    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    body = _envelope(message_id=f"as4-{uuid.uuid4().hex}", payload=_ubl_bytes())

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=body,
            headers={"X-Peppol-Signature": _sign(body)},
        )

    assert resp.status_code == 204
    assert await _count(mk, Invoice) == 0


# ---------------------------------------------------------------------------
# No PII in logs / response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pii_in_logs_or_response(realdb, caplog):
    slug = realdb.info("a").slug
    good = _envelope(message_id=f"as4-{uuid.uuid4().hex}", payload=_ubl_bytes())
    bad_payload = _envelope(message_id=f"as4-{uuid.uuid4().hex}", payload=b"<garbage/>")

    caplog.set_level("DEBUG")
    async with realdb.client(key="a", role=None) as c:
        # success
        r_ok = await c.post(
            f"/api/peppol/inbound/{slug}", content=good, headers={"X-Peppol-Signature": _sign(good)}
        )
        # bad signature
        r_sig = await c.post(
            f"/api/peppol/inbound/{slug}", content=good, headers={"X-Peppol-Signature": "nope"}
        )
        # malformed
        r_mal = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=bad_payload,
            headers={"X-Peppol-Signature": _sign(bad_payload)},
        )

    for r in (r_ok, r_sig, r_mal):
        assert _SUPPLIER_TAX_ID not in r.text
        assert _SENDER_VALUE not in r.text
    # The supplier's participant value / tax id must never reach the log stream.
    assert _SUPPLIER_TAX_ID not in caplog.text
    assert _SENDER_VALUE not in caplog.text


# ---------------------------------------------------------------------------
# Boot guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_refuses_without_secret(monkeypatch):
    from app.config import settings
    from app.main import lifespan

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "a-real-non-default-secret-value")
    monkeypatch.setattr(settings, "peppol_inbound_enabled", True)
    monkeypatch.setattr(settings, "peppol_inbound_signing_secret", "")

    with pytest.raises(RuntimeError, match="FEOH_PEPPOL_INBOUND_SIGNING_SECRET"):
        async with lifespan(object()):  # pragma: no cover - never enters body
            pass


# ---------------------------------------------------------------------------
# Mock adapter parse_inbound unit test
# ---------------------------------------------------------------------------


def test_mock_adapter_parse_inbound():
    from app.services.peppol_adapters.mock_adapter import MockPeppolAdapter

    adapter = MockPeppolAdapter({})
    payload = _ubl_bytes()
    msg = adapter.parse_inbound({}, _envelope(message_id="m-1", payload=payload))
    assert msg is not None
    assert msg.message_id == "m-1"
    assert msg.sender_scheme == _SENDER_SCHEME
    assert msg.sender_value == _SENDER_VALUE
    assert msg.payload == payload

    # Header-metadata + raw-UBL shape also works.
    msg2 = adapter.parse_inbound(
        {
            "X-Peppol-Message-Id": "m-2",
            "X-Peppol-Sender-Scheme": "0192",
            "X-Peppol-Sender-Value": "NO123",
        },
        payload,
    )
    assert msg2 is not None
    assert msg2.message_id == "m-2"
    assert msg2.payload == payload

    # Missing message id → None (can't dedupe → refuse).
    assert adapter.parse_inbound({}, b"<Invoice/>") is None
    assert adapter.parse_inbound({}, b"") is None


# ---------------------------------------------------------------------------
# Workflow instance — the frozen per-invoice snapshot every ingress owes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_invoice_gets_a_frozen_workflow_instance(realdb):
    """A PEPPOL-inbound invoice is a first-class invoice, so it carries the
    same frozen workflow snapshot every other ingress creates.

    Without it the invoice is governed by whatever the *live* definition says
    at read time — so an edit to the tenant's workflow retroactively changes an
    already-ingested invoice, which is exactly what the per-invoice snapshot
    invariant exists to prevent — and it is invisible to every step-based read
    (the approval queue, `GET /api/invoices/{id}/workflow`).
    """
    from app.models.workflow import WorkflowInstance

    mk = realdb.sessionmaker("a")
    slug = realdb.info("a").slug
    message_id = f"as4-{uuid.uuid4().hex}"
    body = _envelope(message_id=message_id, payload=_ubl_bytes())

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/peppol/inbound/{slug}",
            content=body,
            headers={"X-Peppol-Signature": _sign(body)},
        )
    assert resp.status_code == 204

    async with mk() as s:
        inv = (await s.execute(select(Invoice))).scalars().one()
        instances = (
            (await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv.id)))
            .scalars()
            .all()
        )
        assert len(instances) == 1
        instance = instances[0]
        assert instance.state == "active"
        assert instance.correlation_id == inv.correlation_id
        # The snapshot is FROZEN at ingest — a non-empty config copied off the
        # definition, not a null placeholder resolved later.
        assert instance.steps_config_snapshot
