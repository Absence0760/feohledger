"""The processor webhook must reconcile the settled amount, not just the status.

A verified HMAC authenticates the SENDER, not the CONTENT. Before this the
handler took `status: completed` at face value: it stamped the regulated
`completed_at`, captured any accepted early-pay discount off OUR authorized
number, and handed the run to the ERP sync — with no comparison against the
amount AP actually authorized. A wire that left at $50,000 against a $5,000
instruction, a partial settlement, or a mis-mapped provider integration all
reconciled clean, and the only downstream net was a bank statement someone had
to remember to upload days later.

What the handler now does on every `completed` event, and what these tests pin:

  * runs `services/payment_settlement.verify_settlement` against the reported
    amount + currency and the payment's authorized leg(s);
  * records the verdict on the SAME append-only audit row that records the
    money moving — matched, mismatched and unverified alike;
  * on a discrepancy: opens a payment-blocking `fraud_flag` (the same call
    Positive Pay makes for an ALTERED cheque) and does NOT capture the
    discount, while still recording that the payment completed;
  * on a clean settlement: behaves exactly as before.

Unit-level against the real handler coroutine with mocked AsyncSessions,
matching the sibling payment tests' established shape.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.payment_adapters import PaymentStatus

# The fake org's settings carry the webhook secrets; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_request(body: bytes = b"{}", headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {}
    return req


def _ctrl_session_factory(org):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _target_table(stmt) -> str:
    """The table a `select(...)` reads from.

    Dispatching on the TABLE rather than `column_descriptions[0]["entity"]`
    is what makes an aggregate work: `select(func.count()).where(Exception...)`
    has no entity in its column descriptions (the column is the function), so
    an entity-keyed fake silently falls through to the default branch and
    hands back a MagicMock where the handler expects an int.
    """
    try:
        return stmt.get_final_froms()[0].name
    except (AttributeError, IndexError, TypeError):
        return ""


def _tenant_session_factory(payment, invoice, open_exceptions: int = 0):
    """Dispatch results by the statement's target table: `payments` (the
    FOR UPDATE lock), `invoices` (the settlement verifier's target currency +
    the exception's parent), `exceptions` (the fraud_flag dedupe count)."""

    def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        table = _target_table(stmt)
        if table == "invoices":
            result.scalar_one_or_none = MagicMock(return_value=invoice)
        elif table == "exceptions":
            result.scalar = MagicMock(return_value=open_exceptions)
        else:
            result.scalar_one_or_none = MagicMock(return_value=payment)
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, db


def _org(slug="acme", provider="modern_treasury"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        db_name=f"feoh_{slug}",
        settings={"payments": {"provider": provider, "webhook_signing_secret": "s3cret"}},
    )


def _payment(amount=Decimal("5000.00"), **overrides):
    fields = {
        "id": uuid.uuid4(),
        "invoice_id": uuid.uuid4(),
        "correlation_id": uuid.uuid4(),
        "provider_payment_id": "px_1",
        "payment_run_id": uuid.uuid4(),
        "status": "submitted",
        "completed_at": None,
        "reference": None,
        "failure_reason": None,
        "amount": amount,
        "method": "wire",
        "source_amount": None,
        "source_currency": None,
        # Start NULL, as a freshly-submitted row does — so a test asserting
        # "an amount-free rail leaves these alone" is actually meaningful.
        "settled_amount": None,
        "settled_currency": None,
        "settled_amount_unstorable": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _invoice(
    currency="USD",
    amount=Decimal("5000.00"),
    reporting_fx_rate=None,
    reporting_currency="USD",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        currency=currency,
        amount=amount,
        reporting_fx_rate=reporting_fx_rate,
        reporting_currency=reporting_currency,
    )


def _adapter(*, amount, currency, status=PaymentStatus.completed, event_id="evt_1"):
    adapter = MagicMock()
    adapter.parse_webhook = MagicMock(
        return_value=SimpleNamespace(
            provider_payment_id="px_1",
            event_id=event_id,
            status=status,
            reference="REF-1",
            failure_reason=None,
            amount=amount,
            currency=currency,
        )
    )
    return adapter


async def _run(org, adapter, tenant_factory, *, capture=None, exc=None, audit=None, sync=None):
    """Drive the real handler with the patch stack the sibling files use."""
    from app.api.payments import payment_webhook

    capture = capture or AsyncMock()
    exc = exc or AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    audit = audit or AsyncMock()
    sync = sync or AsyncMock()
    with (
        patch("app.database.control_session_factory", _ctrl_session_factory(org)),
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
        patch("app.api.payments._capture_discount_offers", capture),
        patch("app.services.exception_service.create_exception", exc),
        patch("app.services.audit_dispatch.dispatch_audit", audit),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", sync),
    ):
        await payment_webhook(
            tenant_slug=org.slug, provider="modern_treasury", request=_fake_request()
        )
    return {"capture": capture, "exception": exc, "audit": audit, "sync": sync}


# ---------------------------------------------------------------------------
# The clean path stays clean
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_settlement_is_unchanged_behaviour():
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, db = _tenant_session_factory(payment, _invoice())

    mocks = await _run(_org(), _adapter(amount=Decimal("5000.00"), currency="USD"), tenant_factory)

    assert payment.status == "completed"
    assert payment.completed_at is not None
    db.commit.assert_awaited_once()
    # Discount capture runs; no exception raised.
    mocks["capture"].assert_awaited_once()
    mocks["exception"].assert_not_awaited()
    # The run is still handed to the ERP sync.
    mocks["sync"].assert_awaited_once()
    verdict = mocks["audit"].call_args.kwargs["details"]["settlement"]
    assert verdict["outcome"] == "matched"


# ---------------------------------------------------------------------------
# Over-settlement — the headline case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_settlement_opens_a_blocking_fraud_flag_and_skips_the_discount():
    """The wire that left at $50,000 against a $5,000 instruction. The payment
    still records as completed (money moved; refusing to record it does not
    un-move it) but the discount is NOT captured and a payment-blocking
    `fraud_flag` lands in the queue."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, db = _tenant_session_factory(payment, _invoice())

    mocks = await _run(_org(), _adapter(amount=Decimal("50000.00"), currency="USD"), tenant_factory)

    assert payment.status == "completed"
    db.commit.assert_awaited_once()

    mocks["capture"].assert_not_awaited()
    mocks["exception"].assert_awaited_once()
    kwargs = mocks["exception"].call_args.kwargs
    assert kwargs["exception_type"] == "fraud_flag"
    assert kwargs["severity"] == "error"
    assert "50000.00" in kwargs["description"] and "5000.00" in kwargs["description"]


@pytest.mark.asyncio
async def test_over_settlement_records_the_variance_on_the_audit_row():
    """The exception row is mutable and gets resolved; the audit row is the
    append-only, WORM-shipped evidence of what the processor said it moved."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _db = _tenant_session_factory(payment, _invoice())

    mocks = await _run(_org(), _adapter(amount=Decimal("50000.00"), currency="USD"), tenant_factory)

    details = mocks["audit"].call_args.kwargs["details"]
    assert details["status"] == "completed"
    verdict = details["settlement"]
    assert verdict["outcome"] == "amount_mismatch"
    assert verdict["settled_amount"] == "50000.00"
    assert verdict["authorized_amount"] == "5000.00"
    # Positive = the processor moved MORE than we authorized.
    assert verdict["variance"] == "45000.00"


@pytest.mark.asyncio
async def test_under_settlement_is_also_flagged():
    """A partial settlement leaves the supplier short and the invoice not
    actually paid in full — a positive-only check would miss it."""
    payment = _payment(amount=Decimal("500.00"))
    tenant_factory, _db = _tenant_session_factory(payment, _invoice())

    mocks = await _run(_org(), _adapter(amount=Decimal("250.00"), currency="USD"), tenant_factory)

    mocks["exception"].assert_awaited_once()
    assert mocks["audit"].call_args.kwargs["details"]["settlement"]["variance"] == "-250.00"


@pytest.mark.asyncio
async def test_settlement_in_an_unauthorized_currency_is_flagged():
    """1000 EUR and 1000 USD are the same NUMBER — an amount-only comparison
    would call this settled."""
    payment = _payment(amount=Decimal("1000.00"))
    tenant_factory, _db = _tenant_session_factory(payment, _invoice(currency="USD"))

    mocks = await _run(_org(), _adapter(amount=Decimal("1000.00"), currency="EUR"), tenant_factory)

    mocks["exception"].assert_awaited_once()
    assert (
        mocks["audit"].call_args.kwargs["details"]["settlement"]["outcome"] == "currency_mismatch"
    )


# ---------------------------------------------------------------------------
# Cross-currency: the source leg is a legitimate report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_processor_reporting_the_source_leg_of_an_fx_payment_is_not_flagged():
    """A EUR invoice on a USD-home org debits `source_amount` USD. A processor
    reporting the USD side must not be treated as a discrepancy."""
    payment = _payment(
        amount=Decimal("1000.00"),
        source_amount=Decimal("1086.96"),
        source_currency="USD",
    )
    tenant_factory, _db = _tenant_session_factory(payment, _invoice(currency="EUR"))

    mocks = await _run(_org(), _adapter(amount=Decimal("1086.96"), currency="USD"), tenant_factory)

    mocks["exception"].assert_not_awaited()
    mocks["capture"].assert_awaited_once()
    verdict = mocks["audit"].call_args.kwargs["details"]["settlement"]
    assert verdict["outcome"] == "matched"
    assert verdict["authorized_leg"] == "source"


# ---------------------------------------------------------------------------
# Fail-open when the rail reports nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_reporting_no_amount_settles_normally_but_records_the_blind_spot():
    """Dwolla's event body carries no amount. That is not evidence of a
    discrepancy — flagging it would open a fraud_flag on every payment on
    that rail — but it must be VISIBLE on the audit row, not silent."""
    payment = _payment(amount=Decimal("500.00"))
    tenant_factory, db = _tenant_session_factory(payment, _invoice())

    mocks = await _run(_org(), _adapter(amount=None, currency=None), tenant_factory)

    assert payment.status == "completed"
    db.commit.assert_awaited_once()
    mocks["exception"].assert_not_awaited()
    mocks["capture"].assert_awaited_once()
    verdict = mocks["audit"].call_args.kwargs["details"]["settlement"]
    assert verdict["outcome"] == "unverified"
    assert verdict["reason"] == "provider_reported_no_amount"


# ---------------------------------------------------------------------------
# Scope: only a completion is verified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_event_is_not_amount_verified():
    """A `failed` event moved no money, so whatever figure it echoes
    reconciles against nothing — no verdict, no exception, and the invoice
    lookup is skipped entirely."""
    payment = _payment(amount=Decimal("500.00"))
    tenant_factory, db = _tenant_session_factory(payment, _invoice())

    mocks = await _run(
        _org(),
        _adapter(amount=Decimal("999999.00"), currency="USD", status=PaymentStatus.failed),
        tenant_factory,
    )

    assert payment.status == "failed"
    db.commit.assert_awaited_once()
    mocks["exception"].assert_not_awaited()
    mocks["capture"].assert_not_awaited()
    assert "settlement" not in mocks["audit"].call_args.kwargs["details"]


# ---------------------------------------------------------------------------
# Dedupe — one open fraud flag per invoice is the signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_invoice_that_already_carries_an_open_fraud_flag_is_not_double_flagged():
    """Same rule Positive Pay's own return processing uses. A second flag adds
    noise, not information — and the audit row still records the verdict."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _db = _tenant_session_factory(payment, _invoice(), open_exceptions=1)

    mocks = await _run(_org(), _adapter(amount=Decimal("50000.00"), currency="USD"), tenant_factory)

    mocks["exception"].assert_not_awaited()
    # Still not captured, and still on the append-only trail.
    mocks["capture"].assert_not_awaited()
    assert mocks["audit"].call_args.kwargs["details"]["settlement"]["outcome"] == "amount_mismatch"


@pytest.mark.asyncio
async def test_missing_invoice_row_still_settles_and_audits_without_a_queue_entry():
    """A payment whose invoice is gone has nothing to hang the flag on. It
    must not raise — the settlement still records, with an unknown target
    currency treated as a wildcard rather than a manufactured mismatch."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, db = _tenant_session_factory(payment, None)

    mocks = await _run(_org(), _adapter(amount=Decimal("50000.00"), currency="USD"), tenant_factory)

    assert payment.status == "completed"
    db.commit.assert_awaited_once()
    mocks["exception"].assert_not_awaited()
    assert mocks["audit"].call_args.kwargs["details"]["settlement"]["outcome"] == "amount_mismatch"


# ---------------------------------------------------------------------------
# Persisting the settled figure (migration 0083)
# ---------------------------------------------------------------------------
#
# The audit row above is the immutable evidence. These pin the queryable money
# state the ERP sync later reads to decide whether the invoice may be marked
# `paid` (`payment_settlement.settlement_coverage`).


@pytest.mark.asyncio
async def test_settled_figure_is_persisted_on_the_payment_row():
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _ = _tenant_session_factory(payment, _invoice())

    await _run(_org(), _adapter(amount=Decimal("4200.00"), currency="USD"), tenant_factory)

    assert payment.settled_amount == Decimal("4200.00")
    assert payment.settled_currency == "USD"


@pytest.mark.asyncio
async def test_a_clean_settlement_is_persisted_too():
    """Not only discrepancies — the sync needs a figure on every completion to
    tell "covered" apart from "never reported"."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _ = _tenant_session_factory(payment, _invoice())

    await _run(_org(), _adapter(amount=Decimal("5000.00"), currency="USD"), tenant_factory)

    assert payment.settled_amount == Decimal("5000.00")


@pytest.mark.asyncio
async def test_amount_free_rail_leaves_the_columns_null_not_zero():
    """Dwolla's bare envelope. NULL means "no figure on record" and fails OPEN
    in the coverage check; a 0 would read as a total shortfall and hold the
    invoice forever."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _ = _tenant_session_factory(payment, _invoice())

    await _run(_org(), _adapter(amount=None, currency=None), tenant_factory)

    assert payment.settled_amount is None
    assert payment.settled_currency is None


@pytest.mark.asyncio
async def test_a_failed_event_does_not_write_a_settled_figure():
    """A `failed` event moved no money, so whatever figure it echoes
    reconciles against nothing and must not land as a settlement."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _ = _tenant_session_factory(payment, _invoice())

    await _run(
        _org(),
        _adapter(amount=Decimal("5000.00"), currency="USD", status=PaymentStatus.failed),
        tenant_factory,
    )

    assert payment.status == "failed"
    assert payment.settled_amount is None


# ---------------------------------------------------------------------------
# Realized FX gain/loss — the settlement moment is where it becomes real
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_records_realized_fx_gain_loss_for_a_foreign_invoice():
    """multi-currency.md said this happened when a foreign invoice settles.
    Until now nothing computed it — the primitive had no production caller."""
    payment = _payment(
        amount=Decimal("1000.00"),
        source_amount=Decimal("1050.00"),
        source_currency="USD",
    )
    invoice = _invoice(currency="EUR", amount=Decimal("1000.00"), reporting_fx_rate=Decimal("1.10"))
    tenant_factory, _ = _tenant_session_factory(payment, invoice)

    mocks = await _run(_org(), _adapter(amount=Decimal("1000.00"), currency="EUR"), tenant_factory)

    details = mocks["audit"].call_args.kwargs["details"]
    # Accrued 1000 EUR * 1.10 = 1100 USD; paid 1050 USD -> a 50 USD gain.
    assert details["realized_fx_gain_loss"] == "50.00"


@pytest.mark.asyncio
async def test_a_domestic_settlement_records_no_fx_figure_at_all():
    """Absent, not zero — a zero would claim we measured an exposure that
    doesn't exist."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _ = _tenant_session_factory(payment, _invoice())

    mocks = await _run(_org(), _adapter(amount=Decimal("5000.00"), currency="USD"), tenant_factory)

    assert "realized_fx_gain_loss" not in mocks["audit"].call_args.kwargs["details"]


@pytest.mark.asyncio
async def test_no_fx_figure_when_the_accrual_and_the_outflow_use_different_currencies():
    """Group reporting in USD, vendor payments funded from GBP. The accrual and
    the outflow are denominated by two independently-settable org settings, so
    subtracting them would write a confidently wrong number onto the immutable
    audit row."""
    payment = _payment(
        amount=Decimal("1000.00"),
        source_amount=Decimal("880.00"),
        source_currency="GBP",
    )
    invoice = _invoice(
        currency="EUR",
        amount=Decimal("1000.00"),
        reporting_fx_rate=Decimal("1.10"),
        reporting_currency="USD",
    )
    tenant_factory, _ = _tenant_session_factory(payment, invoice)

    mocks = await _run(_org(), _adapter(amount=Decimal("1000.00"), currency="EUR"), tenant_factory)

    assert "realized_fx_gain_loss" not in mocks["audit"].call_args.kwargs["details"]


# ---------------------------------------------------------------------------
# A reported figure the column cannot hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unstorable_settlement_still_records_and_flags_instead_of_5xxing():
    """The headline regression: `payments.settled_amount` is NUMERIC(15, 2), so
    a rail reporting more than 13 integer digits used to parse, verify, and
    then raise `NumericValueOutOfRangeError` at the flush.

    That took the WHOLE transaction with it — the `fraud_flag` the verdict had
    already decided on, and the record that the payment completed at all — the
    handler 5xx'd, and the processor retried into the identical failure. The
    single most suspicious settlement a rail can report was the one the system
    recorded nothing about.

    Everything below is what must survive: the completion, the flag, the
    exception, and the figure itself on the audit row.
    """
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, db = _tenant_session_factory(payment, _invoice())

    mocks = await _run(
        _org(),
        _adapter(amount=Decimal("99999999999999999999.00"), currency="USD"),
        tenant_factory,
    )

    # The money moved; refusing to record it does not un-move it.
    assert payment.status == "completed"
    assert payment.completed_at is not None
    db.commit.assert_awaited_once()

    # Nothing unstorable was assigned to the NUMERIC column…
    assert payment.settled_amount is None
    # …but "we were told something we cannot represent" is NOT the same fact as
    # "nothing was reported", and the row has to keep them apart.
    assert payment.settled_amount_unstorable is True
    assert payment.settled_currency == "USD"

    # The verdict still stands, so the payment-blocking flag still lands.
    mocks["capture"].assert_not_awaited()
    mocks["exception"].assert_awaited_once()
    assert mocks["exception"].call_args.kwargs["exception_type"] == "fraud_flag"

    # And the figure survives verbatim where there is no range limit.
    verdict = mocks["audit"].call_args.kwargs["details"]["settlement"]
    assert verdict["outcome"] == "amount_mismatch"
    assert verdict["settled_amount"] == "99999999999999999999.00"


@pytest.mark.asyncio
async def test_a_storable_settlement_never_sets_the_unstorable_flag():
    """The flag must stay off on the ordinary path — otherwise every settled
    invoice would hold at `payment_scheduled`."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _db = _tenant_session_factory(payment, _invoice())

    await _run(_org(), _adapter(amount=Decimal("5000.00"), currency="USD"), tenant_factory)

    assert payment.settled_amount == Decimal("5000.00")
    assert payment.settled_amount_unstorable is False


@pytest.mark.asyncio
async def test_an_amount_free_rail_does_not_set_the_unstorable_flag():
    """Dwolla's bare envelope reports nothing. That is a blind spot, not a
    garbage report — conflating them would hold every invoice such a rail
    settles, which is precisely what the NULL-fails-open rule exists to avoid."""
    payment = _payment(amount=Decimal("5000.00"))
    tenant_factory, _db = _tenant_session_factory(payment, _invoice())

    await _run(_org(), _adapter(amount=None, currency=None), tenant_factory)

    assert payment.settled_amount is None
    assert payment.settled_amount_unstorable is False
