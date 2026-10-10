"""In-depth security tests for the payment-processor webhook handler.

Per project invariant #9: "Webhook handlers verify signatures and
dedupe by event id." The handler at `POST /api/payments/webhook/
{tenant_slug}/{provider}` is authenticated purely by the processor's
HMAC signature — there's no JWT, no header, no cookie. A regression
here is "Critical" because it turns a one-time effect (a settled
payment) into a replayable money-mover.

We mock the DB sessions but exercise the real handler shape:
   - Unknown tenant slug → silent 204 (no info leak)
   - Wrong provider for tenant → silent 204
   - Bad signature (parse_webhook returns None) → silent 204
   - Unknown provider_payment_id → silent 204 (late retry of a payment
     we don't have)
   - Terminal payment is not downgraded on a re-delivered webhook
   - ERP sync only fires when a payment newly settled
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The fake org's settings carry the webhook secrets; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")


def _fake_request(body: bytes = b"{}", headers: dict | None = None):
    """Minimal mock of the FastAPI `Request` the handler reads from."""
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {}
    return req


def _ctrl_session_factory(org):
    """Mock the control-plane session factory used to look up the org."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _target_table(stmt) -> str:
    """The table a `select(...)` reads from, or "" if it can't be resolved.

    The settle path issues more than one query (Payment FOR UPDATE, then the
    Invoice whose currency the settlement verifier compares against, then the
    fraud_flag dedupe count), so a factory that returns one canned row for
    every `execute` hands the Payment back as an Invoice. Dispatching on the
    TABLE — not `column_descriptions[0]["entity"]`, which is empty for an
    aggregate like `select(func.count())` — keeps the fake honest as the
    handler grows.
    """
    try:
        return stmt.get_final_froms()[0].name
    except (AttributeError, IndexError, TypeError):
        return ""


def _tenant_session_factory(payment, invoice=None, open_exceptions=0):
    """Mock the tenant-DB session factory used to look up / mutate
    the Payment row."""

    def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        table = _target_table(stmt)
        if table == "invoices":
            result.scalar_one_or_none = MagicMock(return_value=invoice)
        elif table == "exceptions":
            # The `_open_settlement_mismatch_exception` dedupe count.
            result.scalar = MagicMock(return_value=open_exceptions)
        else:
            result.scalar_one_or_none = MagicMock(return_value=payment)
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    # `AsyncSession.add` is SYNCHRONOUS. AsyncMock auto-specs every attribute as
    # async, so leaving it makes `db.add(...)` (audit.py, on the settle path)
    # return a coroutine nobody awaits — a RuntimeWarning that masks nothing but
    # obscures real ones.
    db.add = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, db


def _payment(
    *,
    status="submitted",
    amount=Decimal("500.00"),
    provider_payment_id="px_1",
    payment_run_id=None,
    **overrides,
):
    """A Payment stand-in carrying every field the settle path reads.

    Kept in one place so a new column the handler starts reading is added
    once, not per test — a stale fixture that silently lacks an attribute is
    how a mocked suite drifts away from the real row.
    """
    fields = {
        "id": uuid.uuid4(),
        "invoice_id": uuid.uuid4(),
        "correlation_id": uuid.uuid4(),
        "provider_payment_id": provider_payment_id,
        "payment_run_id": payment_run_id,
        "status": status,
        "completed_at": None,
        "reference": None,
        "failure_reason": None,
        "amount": amount,
        "method": "ach",
        # International legs — NULL on a domestic payment.
        "source_amount": None,
        "source_currency": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _event(
    *,
    provider_payment_id="px_1",
    event_id="evt_1",
    status,
    reference=None,
    failure_reason=None,
    amount=None,
    currency=None,
):
    """A `WebhookEvent` stand-in. `amount`/`currency` default to None — the
    `unverified` settlement branch — so a test opts in to the verification."""
    return SimpleNamespace(
        provider_payment_id=provider_payment_id,
        event_id=event_id,
        status=status,
        reference=reference,
        failure_reason=failure_reason,
        amount=amount,
        currency=currency,
    )


def _invoice(
    currency="USD", amount=Decimal("5000.00"), reporting_currency="USD", reporting_fx_rate=None
):
    """Models the Invoice columns the webhook handler actually reads.

    `amount` / `reporting_currency` / `reporting_fx_rate` feed the realized-FX
    figure recorded on the settlement audit row; a real ORM row always carries
    them, so omitting them here made the fake diverge from production rather
    than exercise it.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        currency=currency,
        amount=amount,
        reporting_currency=reporting_currency,
        reporting_fx_rate=reporting_fx_rate,
    )


def _org(slug="acme", provider="modern_treasury", webhook_secret="s3cret"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        db_name=f"feoh_{slug}",
        settings={
            "payments": {
                "provider": provider,
                "webhook_signing_secret": webhook_secret,
            }
        },
    )


# ---------------------------------------------------------------------------
# Silent rejection for "no such tenant"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_returns_silently_for_unknown_tenant():
    """A request to /api/payments/webhook/<garbage>/modern_treasury
    must not leak the fact that the tenant doesn't exist. Returning
    a different status / error for missing tenants would let an
    attacker enumerate slugs."""
    from app.api.payments import payment_webhook

    with (
        patch(
            "app.database.control_session_factory",
            _ctrl_session_factory(None),  # tenant not found
        ),
        # Adapter resolution must not be reached.
        patch("app.api.payments.get_payment_adapter") as mk_adapter,
    ):
        result = await payment_webhook(
            tenant_slug="does-not-exist",
            provider="modern_treasury",
            request=_fake_request(),
        )

    assert result is None  # 204 No Content path
    mk_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_returns_silently_for_wrong_provider():
    """A tenant configured for `modern_treasury` receives a webhook
    POSTed to /.../mock — the handler must silently 204 rather than
    dispatching to the wrong adapter (which could mis-parse the body
    and corrupt state)."""
    from app.api.payments import payment_webhook

    org = _org(provider="modern_treasury")
    with (
        patch(
            "app.database.control_session_factory",
            _ctrl_session_factory(org),
        ),
        patch("app.api.payments.get_payment_adapter") as mk_adapter,
    ):
        result = await payment_webhook(
            tenant_slug="acme",
            provider="mock",  # wrong adapter for this tenant
            request=_fake_request(),
        )

    assert result is None
    mk_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_rejects_mock_provider_before_tenant_lookup():
    """The `mock` adapter's `parse_webhook` does NO signature verification and
    `mock` is the default provider for un-configured tenants (seeded demos +
    fresh signups). Serving it on this public route would accept forged status
    transitions, so the handler must reject `provider=="mock"` outright — before
    even resolving the tenant — regardless of what the tenant is configured for.
    """
    from app.api.payments import payment_webhook

    # Even a tenant explicitly CONFIGURED for mock must be refused: the provider
    # cross-check would otherwise pass and reach the unauthenticated parse.
    org = _org(provider="mock")
    with (
        patch("app.database.control_session_factory") as mk_ctrl,
        patch("app.api.payments.get_payment_adapter") as mk_adapter,
    ):
        result = await payment_webhook(
            tenant_slug="acme",
            provider="mock",
            request=_fake_request(body=b'{"provider_payment_id":"px","status":"completed"}'),
        )

    assert result is None  # 204 No Content path
    # Rejected before any tenant lookup or adapter resolution.
    mk_ctrl.assert_not_called()
    mk_adapter.assert_not_called()
    _ = org  # constructed to document the "configured for mock" case


@pytest.mark.asyncio
async def test_webhook_rejects_unsupported_configured_provider():
    """The `provider == "mock"` guard above only catches the literal name.

    A tenant whose `settings.payments.provider` is a name we have no adapter
    for used to resolve to the mock adapter anyway — `get_payment_adapter`
    fell back to it — so a typo like `modern-treasury` re-opened exactly the
    hole that guard closes, under a different URL segment: the request passes
    the provider cross-check (config matches the path) and then reaches
    `mock.parse_webhook`, which verifies NO signature. The dispatcher now
    fails closed and the handler 204s, so no unverified parse happens.

    Uses the REAL dispatcher (no `get_payment_adapter` patch) — patching it
    would mock away the behaviour under test.
    """
    from app.api.payments import payment_webhook

    org = _org(provider="modern-treasury")  # hyphen — not a registered adapter
    tenant_factory, db = _tenant_session_factory(payment=None)

    with (
        patch(
            "app.database.control_session_factory",
            _ctrl_session_factory(org),
        ),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        result = await payment_webhook(
            tenant_slug="acme",
            provider="modern-treasury",
            request=_fake_request(body=b'{"provider_payment_id":"px","status":"completed"}'),
        )

    assert result is None  # 204 No Content path
    # The forged body must never reach a parser, and the tenant DB must never
    # be opened — the same posture as a failed signature check.
    db.execute.assert_not_called()
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_returns_silently_when_adapter_rejects_signature():
    """`parse_webhook(headers, body)` returns None for any non-
    verifiable payload: bad HMAC, malformed body, unrecognised event
    type. The handler must NOT touch the DB in that case."""
    from app.api.payments import payment_webhook

    org = _org()
    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(return_value=None)  # signature failed

    tenant_factory, db = _tenant_session_factory(payment=None)

    with (
        patch(
            "app.database.control_session_factory",
            _ctrl_session_factory(org),
        ),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        result = await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(body=b'{"forged":"payload"}'),
        )

    assert result is None
    # CRITICAL: the tenant DB must not be opened on a bad signature.
    # Without this, a flood of forged webhooks would still hammer Postgres.
    db.execute.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_passes_headers_and_body_to_adapter_unchanged():
    """The adapter is the HMAC oracle — the handler must forward the
    raw body and headers verbatim. Mutating either would break signature
    verification (the processor signed the bytes-on-the-wire, not a
    parsed copy)."""
    from app.api.payments import payment_webhook

    org = _org()
    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(return_value=None)

    raw_body = b'{"event":"payment_order.settled","id":"evt_1"}'
    headers = {"x-modern-treasury-signature": "abc123", "content-type": "application/json"}

    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(body=raw_body, headers=headers),
        )

    adapter.parse_webhook.assert_called_once()
    call_headers, call_body = adapter.parse_webhook.call_args[0]
    assert call_body == raw_body, "body must be forwarded byte-identical"
    assert call_headers["x-modern-treasury-signature"] == "abc123"


# ---------------------------------------------------------------------------
# Dedup / replay protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_does_not_downgrade_terminal_payment():
    """A re-delivered webhook for a payment that already reached
    `completed` (or `failed`, `cancelled`) must NOT roll the status
    back. Processors re-send on any non-2xx, sometimes hours apart;
    the same event id can land twice."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org()
    completed = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_1",
        payment_run_id=uuid.uuid4(),
        status="completed",
        completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        reference="REF-OLD",
        failure_reason=None,
    )

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=SimpleNamespace(
            provider_payment_id="px_1",
            event_id="evt_1",
            status=PaymentStatus.submitted,  # downgrade attempt
            reference="REF-NEW",
            failure_reason=None,
        )
    )

    tenant_factory, db = _tenant_session_factory(completed)

    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        patch("app.services.payment_erp_sync.dispatch_payment_sync") as mk_sync,
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    # Status untouched, no commit, no ERP fan-out.
    assert completed.status == "completed"
    assert completed.reference == "REF-OLD"
    db.commit.assert_not_called()
    mk_sync.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_does_not_downgrade_failed_payment():
    """Same dedup contract on the `failed` terminal."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org()
    failed = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_2",
        payment_run_id=uuid.uuid4(),
        status="failed",
        completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        reference=None,
        failure_reason="insufficient_funds",
    )

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=SimpleNamespace(
            provider_payment_id="px_2",
            event_id="evt_2",
            status=PaymentStatus.completed,  # late "settled" arriving after failure
            reference="REF-NEW",
            failure_reason=None,
        )
    )

    tenant_factory, db = _tenant_session_factory(failed)
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    assert failed.status == "failed"
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_does_not_resurrect_voided_payment():
    """BUG C regression. A `voided` payment is terminal — a late /
    re-delivered `completed` event (e.g. the rail confirmed settlement
    moments before AP voided it, but the webhook landed after) must NOT
    flip it back to `completed`. That would silently re-enable money the
    operator deliberately reversed, with no audit row recording the flip.

    Before the fix the handler used a *blocklist* (`completed`/`failed`/
    `cancelled`) that omitted `voided`, so this event was applied. The
    fix switches to an *allowlist* of in-flight statuses, so any state
    not in {pending, submitted, processing} is left untouched.
    """
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org()
    voided = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_void",
        payment_run_id=uuid.uuid4(),
        status="voided",
        completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        reference="REF-VOID",
        failure_reason="Voided by AP: duplicate",
    )

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=SimpleNamespace(
            provider_payment_id="px_void",
            event_id="evt_void",
            status=PaymentStatus.completed,  # late "settled" after the void
            reference="REF-NEW",
            failure_reason=None,
        )
    )

    tenant_factory, db = _tenant_session_factory(voided)
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        patch("app.services.payment_erp_sync.dispatch_payment_sync") as mk_sync,
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    # Status + reason untouched, no commit, no ERP fan-out.
    assert voided.status == "voided"
    assert voided.reference == "REF-VOID"
    db.commit.assert_not_called()
    mk_sync.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_does_not_resurrect_pending_compliance_payment():
    """A `pending_compliance` hold is a deliberate AP-review state, not
    an in-flight processor state. A `completed` webhook must not clear
    the hold and mark it settled behind the reviewer's back."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org()
    held = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_hold",
        payment_run_id=uuid.uuid4(),
        status="pending_compliance",
        completed_at=None,
        reference=None,
        failure_reason="compliance_hold: manual review",
    )

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=SimpleNamespace(
            provider_payment_id="px_hold",
            event_id="evt_hold",
            status=PaymentStatus.completed,
            reference="REF-NEW",
            failure_reason=None,
        )
    )

    tenant_factory, db = _tenant_session_factory(held)
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        patch("app.services.payment_erp_sync.dispatch_payment_sync") as mk_sync,
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    assert held.status == "pending_compliance"
    db.commit.assert_not_called()
    mk_sync.assert_not_called()


# ---------------------------------------------------------------------------
# Unknown payment / late delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_silently_ignores_unknown_provider_payment_id():
    """A webhook whose `provider_payment_id` doesn't match any row
    must be a no-op — could be a payment we deleted, a test event the
    processor sent before integration, or a malicious crafted body.
    Either way, return 204 with no DB mutation."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org()
    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=SimpleNamespace(
            provider_payment_id="px_unknown",
            event_id="evt_unknown",
            status=PaymentStatus.completed,
            reference=None,
            failure_reason=None,
        )
    )

    tenant_factory, db = _tenant_session_factory(payment=None)  # no row
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Tenant isolation via URL path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_only_touches_the_url_path_tenant_db():
    """The tenant comes from the URL path, never from the body. A
    payload that names a different org_id in its metadata must NOT
    redirect the update to that org's DB."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org(slug="acme")
    payment = _payment(provider_payment_id="px_3")

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=_event(
            provider_payment_id="px_3",
            event_id="evt_3",
            status=PaymentStatus.completed,
            reference="REF-1",
        )
    )

    tenant_factory, db = _tenant_session_factory(payment, invoice=_invoice())
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine") as mk_engine,
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            # The body claims to be for "techflow" — the handler must ignore that.
            request=_fake_request(body=b'{"organization_id":"techflow"}'),
        )

    # The engine was requested for acme's DB, not techflow's.
    assert mk_engine.call_count == 1
    assert mk_engine.call_args[0][0] == "feoh_acme"


# ---------------------------------------------------------------------------
# Audit trail (project invariant: a money-status change touching the
# regulated `completed_at` must produce an append-only audit row).
# The processor's webhook is the production path that settles a payment.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_settle_writes_audit_row():
    """A `submitted → completed` webhook stamps the regulated `completed_at`,
    so it MUST write a `payment.completed` audit row. Before the fix the
    handler flipped the status + timestamp with no audit row at all — the
    one place the real processor confirms money moved was untraced."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org(slug="acme")
    payment = _payment(
        provider_payment_id="px_settle",
        payment_run_id=uuid.uuid4(),
        amount=Decimal("1234.56"),
    )

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=_event(
            provider_payment_id="px_settle",
            event_id="evt_settle",
            status=PaymentStatus.completed,
            reference="REF-OK",
            amount=Decimal("1234.56"),
            currency="USD",
        )
    )

    tenant_factory, db = _tenant_session_factory(payment, invoice=_invoice())
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        patch("app.services.audit_dispatch.dispatch_audit") as mk_audit,
        patch("app.services.payment_erp_sync.dispatch_payment_sync"),
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    assert payment.status == "completed"
    db.commit.assert_called_once()
    mk_audit.assert_called_once()
    kwargs = mk_audit.call_args.kwargs
    assert kwargs["action"] == "payment.completed"
    assert kwargs["entity_type"] == "payment"
    assert kwargs["entity_id"] == payment.id
    assert kwargs["organization_id"] == org.id
    # System-initiated (the processor, not a user).
    assert kwargs["actor_id"] is None
    details = kwargs["details"]
    assert details["status"] == "completed"
    assert details["previous_status"] == "submitted"
    assert details["source"] == "webhook"
    # PII-free: amount is the Decimal serialised to a string, no bank values.
    assert details["amount"] == "1234.56"
    assert "iban" not in details and "account" not in details
    # The settlement verdict rides the same append-only row — this is the
    # WORM-shipped evidence of what the processor said it moved.
    assert details["settlement"]["outcome"] == "matched"
    assert details["settlement"]["settled_amount"] == "1234.56"
    assert details["settlement"]["authorized_amount"] == "1234.56"


@pytest.mark.asyncio
async def test_webhook_rejected_path_writes_no_audit_row():
    """A re-delivered `completed` against an already-terminal `voided`
    payment must NOT write an audit row — nothing changed, so the trail
    stays clean (and no commit fires)."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus

    org = _org()
    voided = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_void2",
        payment_run_id=uuid.uuid4(),
        status="voided",
        completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        reference="REF-VOID",
        failure_reason="Voided by AP: duplicate",
        correlation_id=uuid.uuid4(),
        amount=Decimal("10.00"),
        method="ach",
    )

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=SimpleNamespace(
            provider_payment_id="px_void2",
            event_id="evt_void2",
            status=PaymentStatus.completed,
            reference="REF-NEW",
            failure_reason=None,
        )
    )

    tenant_factory, db = _tenant_session_factory(voided)
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        patch("app.services.audit_dispatch.dispatch_audit") as mk_audit,
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    assert voided.status == "voided"
    db.commit.assert_not_called()
    mk_audit.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup-claim release on failure (so the provider's retry can reprocess).
# The handler claims a Redis dedup slot for the event id, then does the
# status change. If that DB work raises, the claim is only durable over a
# rolled-back txn — leaving it set would dedup the retry away for the full
# TTL and the payment would never reach terminal status. The handler must
# release the claim and re-raise (a 5xx → the provider retries).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_releases_claim_when_db_block_raises(_autouse_fake_redis):
    """A failure AFTER the event is claimed releases the claim so a redelivery
    reprocesses (and re-raises so the provider actually retries)."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus
    from app.services.webhook_security import is_event_already_processed

    org = _org(slug="acme")
    payment = _payment(provider_payment_id="px_fail", amount=Decimal("42.00"))

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=_event(
            provider_payment_id="px_fail",
            event_id="evt_fail",
            status=PaymentStatus.completed,
            reference="REF",
        )
    )

    tenant_factory, db = _tenant_session_factory(payment, invoice=_invoice())
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        # Force the tenant-DB block to raise AFTER the event was claimed.
        patch(
            "app.services.audit_dispatch.dispatch_audit",
            side_effect=RuntimeError("boom"),
        ),
    ):
        # The handler must re-raise so the provider gets a non-2xx and retries.
        with pytest.raises(RuntimeError):
            await payment_webhook(
                tenant_slug="acme",
                provider="modern_treasury",
                request=_fake_request(),
            )

    # Claim released: a fresh dedup check returns False (i.e. the retry is NOT
    # deduped away — it would reprocess). Before the fix this returned True.
    assert await is_event_already_processed("modern_treasury", "evt_fail") is False


@pytest.mark.asyncio
async def test_webhook_keeps_claim_on_success(_autouse_fake_redis):
    """Contrast: on a clean settle the claim is RETAINED, so a genuine
    redelivery of the same event is deduped away (the normal dedup contract)."""
    from app.api.payments import payment_webhook
    from app.services.payment_adapters import PaymentStatus
    from app.services.webhook_security import is_event_already_processed

    org = _org(slug="acme")
    payment = _payment(provider_payment_id="px_ok", amount=Decimal("42.00"))

    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=_event(
            provider_payment_id="px_ok",
            event_id="evt_ok",
            status=PaymentStatus.completed,
            reference="REF",
        )
    )

    tenant_factory, db = _tenant_session_factory(payment, invoice=_invoice())
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        patch("app.services.audit_dispatch.dispatch_audit"),
        patch("app.services.payment_erp_sync.dispatch_payment_sync"),
    ):
        await payment_webhook(
            tenant_slug="acme",
            provider="modern_treasury",
            request=_fake_request(),
        )

    db.commit.assert_called_once()
    # Claim retained → a redelivery of evt_ok is already-processed (deduped).
    assert await is_event_already_processed("modern_treasury", "evt_ok") is True


# ---------------------------------------------------------------------------
# Body-size cap (memory-exhaustion DoS on a public route)
#
# `payment_webhook` used to `await request.body()` with no size cap, ahead of
# even the `mock`-provider check and tenant/HMAC resolution — an
# unauthenticated attacker could POST an arbitrarily large body and have it
# buffered fully into memory before anything ever rejected it. The guard
# bounds the body in two phases, mirroring `erp_webhook`/`peppol_inbound`:
# reject on a declared Content-Length over the cap BEFORE reading the body at
# all, then re-check the actual read length in case the header lied or was
# absent (e.g. chunked transfer).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_length_over_cap_rejects_before_body_read(monkeypatch):
    """A declared Content-Length over the cap must reject WITHOUT ever
    awaiting `request.body()` — the whole point is bounding memory before
    anything is buffered."""
    from app.api.payments import payment_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "payment_webhook_max_bytes", 1024)
    request = _fake_request(b"", {"content-length": "999999"})

    result = await payment_webhook(tenant_slug="acme", provider="modern_treasury", request=request)

    assert result is None  # silent 204, not a raised exception
    request.body.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_length_malformed_rejects_before_body_read(monkeypatch):
    """A non-integer Content-Length header must also reject before reading —
    a malformed header shouldn't fall through to an unbounded read."""
    from app.api.payments import payment_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "payment_webhook_max_bytes", 1024)
    request = _fake_request(b"", {"content-length": "not-a-number"})

    result = await payment_webhook(tenant_slug="acme", provider="modern_treasury", request=request)

    assert result is None
    request.body.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_body_without_content_length_rejects_after_read(monkeypatch):
    """Simulates chunked transfer (no Content-Length header): the body is
    read once, then rejected by the post-read length check."""
    from app.api.payments import payment_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "payment_webhook_max_bytes", 1024)
    big_body = b"x" * 2048
    request = _fake_request(big_body, {})

    result = await payment_webhook(tenant_slug="acme", provider="modern_treasury", request=request)

    assert result is None
    request.body.assert_awaited_once()


@pytest.mark.asyncio
async def test_content_length_understates_actual_size_still_rejects(monkeypatch):
    """A Content-Length header that lies (understates the real body) must
    still be caught by the post-read re-check, not trusted blindly."""
    from app.api.payments import payment_webhook
    from app.config import settings

    monkeypatch.setattr(settings, "payment_webhook_max_bytes", 1024)
    big_body = b"x" * 2048
    request = _fake_request(big_body, {"content-length": "10"})

    result = await payment_webhook(tenant_slug="acme", provider="modern_treasury", request=request)

    assert result is None
    request.body.assert_awaited_once()
