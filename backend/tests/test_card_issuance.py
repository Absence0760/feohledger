"""Unit tests for `services.card_issuance.issue_card_for_invoice`.

The helper lives between the payment-run executor and the card-adapter
dispatcher. The tests below pin its three failure shapes (cards-not-
enabled, adapter raises, adapter returns failure) and the happy-path
mapping from adapter result onto a fresh `VirtualCard` row.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _invoice():
    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        vendor_name="Acme Supplies",
        amount=Decimal("1234.56"),
        currency="USD",
        description="Q1 office supplies",
        invoice_number="INV-42",
    )


def _db(existing_cards: int = 0):
    """Minimal AsyncSession stand-in.

    `issue_card_for_invoice` only reads one thing off the session: how many
    VirtualCard rows the invoice already has (the re-issue discriminator in the
    provider idempotency key).
    """
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=existing_cards)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _app_settings():
    return SimpleNamespace(
        lithic_api_key="lithic-k",
        lithic_sandbox=True,
        nium_client_id="nium-c",
        nium_client_secret="nium-s",
        nium_customer_hash_id="cust",
        nium_wallet_hash_id="wallet",
        nium_sandbox=True,
    )


@pytest.mark.asyncio
async def test_issue_card_returns_failure_when_org_has_not_enabled_cards():
    """A row whose method is `virtual_card` but whose org never turned
    on the card program shouldn't crash the run — the executor wants
    a structured "skip this row" signal."""
    from app.services.card_issuance import issue_card_for_invoice

    result = await issue_card_for_invoice(
        db=_db(),
        invoice=_invoice(),
        organization_id=uuid.uuid4(),
        org_settings={"cards": {"enabled": False}},
        app_settings=_app_settings(),
    )

    assert result.success is False
    assert result.card is None
    assert result.failure_reason == "cards_not_enabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "  "])
async def test_issue_card_refuses_an_invoice_with_no_currency(blank):
    """A card is denominated in the invoice's currency. One that records none is
    refused before any provider call, never minted as USD."""
    from app.services.card_issuance import issue_card_for_invoice

    invoice = _invoice()
    invoice.currency = blank
    adapter = MagicMock()
    adapter.create_card = AsyncMock()
    with patch("app.services.card_adapters.get_card_adapter", return_value=adapter):
        result = await issue_card_for_invoice(
            db=_db(),
            invoice=invoice,
            organization_id=uuid.uuid4(),
            org_settings={"cards": {"enabled": True, "program_type": "platform", "region": "US"}},
            app_settings=_app_settings(),
        )

    assert result.success is False
    assert result.card is None
    assert result.failure_reason == "currency_unknown"
    adapter.create_card.assert_not_called()


@pytest.mark.asyncio
async def test_issue_card_returns_failure_when_settings_missing_cards_key():
    """`org_settings={}` is the same business case as `cards.enabled=False`
    — never enabled. Belt-and-braces because callers can hand us None too."""
    from app.services.card_issuance import issue_card_for_invoice

    result = await issue_card_for_invoice(
        db=_db(),
        invoice=_invoice(),
        organization_id=uuid.uuid4(),
        org_settings={},
        app_settings=_app_settings(),
    )

    assert result.success is False
    assert result.failure_reason == "cards_not_enabled"


@pytest.mark.asyncio
async def test_issue_card_swallows_adapter_exceptions_as_structured_failure():
    """A flaky card provider must not raise out of the executor — the
    row should be skipped with `adapter_error:<ClassName>` so the run
    can finish and the operator can chase the rest by hand."""
    from app.services.card_issuance import issue_card_for_invoice

    adapter = MagicMock()
    adapter.provider_name = "lithic"
    adapter.create_card = AsyncMock(side_effect=RuntimeError("503 from upstream"))

    with patch("app.services.card_adapters.get_card_adapter", return_value=adapter):
        result = await issue_card_for_invoice(
            db=_db(),
            invoice=_invoice(),
            organization_id=uuid.uuid4(),
            org_settings={"cards": {"enabled": True, "program_type": "platform", "region": "US"}},
            app_settings=_app_settings(),
        )

    assert result.success is False
    assert result.card is None
    assert result.failure_reason == "adapter_error:RuntimeError"


@pytest.mark.asyncio
async def test_issue_card_returns_failure_reason_from_adapter():
    """The adapter is the source of truth for business-level failures
    (e.g. KYC reject). The helper forwards `result.failure_reason` so
    the audit row is informative."""
    from app.services.card_issuance import issue_card_for_invoice

    adapter = MagicMock()
    adapter.provider_name = "lithic"
    adapter.create_card = AsyncMock(
        return_value=SimpleNamespace(
            success=False,
            failure_reason="insufficient_funds",
            provider_card_id=None,
            last_four=None,
        )
    )

    with patch("app.services.card_adapters.get_card_adapter", return_value=adapter):
        result = await issue_card_for_invoice(
            db=_db(),
            invoice=_invoice(),
            organization_id=uuid.uuid4(),
            org_settings={"cards": {"enabled": True, "program_type": "platform", "region": "US"}},
            app_settings=_app_settings(),
        )

    assert result.success is False
    assert result.failure_reason == "insufficient_funds"


@pytest.mark.asyncio
async def test_issue_card_happy_path_builds_virtual_card_row():
    """On adapter success, the helper hands back an uncommitted
    `VirtualCard` row stamped with the provider's identifiers and the
    invoice's correlation id (needed for webhook reconciliation)."""
    from app.services.card_issuance import issue_card_for_invoice

    inv = _invoice()
    payment_id = uuid.uuid4()
    org_id = uuid.uuid4()

    adapter = MagicMock()
    adapter.provider_name = "lithic"
    adapter.create_card = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            failure_reason=None,
            provider_card_id="card_abc123",
            last_four="4242",
        )
    )

    with patch("app.services.card_adapters.get_card_adapter", return_value=adapter):
        result = await issue_card_for_invoice(
            db=_db(),
            invoice=inv,
            organization_id=org_id,
            org_settings={"cards": {"enabled": True, "program_type": "platform", "region": "US"}},
            app_settings=_app_settings(),
            payment_id=payment_id,
        )

    assert result.success is True
    assert result.card is not None
    card = result.card
    assert card.invoice_id == inv.id
    assert card.payment_id == payment_id
    assert card.organization_id == org_id
    assert card.correlation_id == inv.correlation_id
    assert card.card_provider == "lithic"
    assert card.provider_card_id == "card_abc123"
    assert card.last_four == "4242"
    assert card.amount_limit == inv.amount
    assert card.currency == "USD"
    assert card.status == "created"


@pytest.mark.asyncio
async def test_issue_card_uses_explicit_amount_when_provided():
    """The executor passes an explicit `amount` when the payment row
    differs from the invoice total (e.g., early-pay discount). The
    helper must forward that, not the invoice's gross."""
    from app.services.card_issuance import issue_card_for_invoice

    inv = _invoice()
    explicit_amount = Decimal("999.00")

    adapter = MagicMock()
    adapter.provider_name = "lithic"
    captured_payload = {}

    async def capture(payload):
        captured_payload["amount"] = payload.amount
        return SimpleNamespace(
            success=True,
            failure_reason=None,
            provider_card_id="card_x",
            last_four="0000",
        )

    adapter.create_card = AsyncMock(side_effect=capture)

    with patch("app.services.card_adapters.get_card_adapter", return_value=adapter):
        result = await issue_card_for_invoice(
            db=_db(),
            invoice=inv,
            organization_id=uuid.uuid4(),
            org_settings={"cards": {"enabled": True, "program_type": "platform", "region": "US"}},
            app_settings=_app_settings(),
            amount=explicit_amount,
        )

    assert captured_payload["amount"] == explicit_amount
    assert result.card is not None
    assert result.card.amount_limit == explicit_amount


# ------------------------------------------------- provider idempotency ----
#
# A virtual card is spendable money, so issuance carries the same idempotency
# obligation as a payment. The DB index uq_virtual_cards_one_live_per_invoice
# only catches duplicates that reached OUR database: when httpx times out AFTER
# the provider provisioned the card, nothing is persisted, and an unkeyed retry
# mints a SECOND live card while the first sits orphaned and ungoverned. These
# pin the stable key that closes that hole.


async def _capture_payload(inv, *, db, **kwargs):
    """Run an issuance against a capturing adapter, return the payload sent."""
    from app.services.card_issuance import issue_card_for_invoice

    seen = {}

    async def capture(payload):
        seen["payload"] = payload
        return SimpleNamespace(
            success=True,
            failure_reason=None,
            provider_card_id="card_x",
            last_four="0000",
        )

    adapter = MagicMock()
    adapter.provider_name = "lithic"
    adapter.create_card = AsyncMock(side_effect=capture)

    with patch("app.services.card_adapters.get_card_adapter", return_value=adapter):
        await issue_card_for_invoice(
            db=db,
            invoice=inv,
            organization_id=uuid.uuid4(),
            org_settings={"cards": {"enabled": True, "program_type": "platform", "region": "US"}},
            app_settings=_app_settings(),
            **kwargs,
        )
    return seen["payload"]


@pytest.mark.asyncio
async def test_issue_card_sends_an_idempotency_key_to_the_provider():
    """Every card creation must carry a provider idempotency key — without it
    a timed-out create is unrecoverable (orphaned live card + duplicate)."""
    payload = await _capture_payload(_invoice(), db=_db())

    assert payload.idempotency_key
    # Lithic rejects a non-UUID Idempotency-Key, so the shared key must parse.
    uuid.UUID(payload.idempotency_key)


@pytest.mark.asyncio
async def test_idempotency_key_is_stable_across_retries_of_the_same_issuance():
    """THE bug: attempt 1 times out after the provider made the card, so
    nothing is persisted and AP retries. The retry must present the SAME key so
    the provider replays the original card instead of minting a second."""
    inv = _invoice()

    first = await _capture_payload(inv, db=_db(existing_cards=0))
    # Nothing persisted by the timed-out attempt → the retry sees the same state.
    retry = await _capture_payload(inv, db=_db(existing_cards=0))

    assert first.idempotency_key == retry.idempotency_key


@pytest.mark.asyncio
async def test_idempotency_key_differs_per_invoice():
    """Two payables are two issuances — sharing a key would make the second
    invoice silently reuse the first invoice's card."""
    a = await _capture_payload(_invoice(), db=_db())
    b = await _capture_payload(_invoice(), db=_db())

    assert a.idempotency_key != b.idempotency_key


@pytest.mark.asyncio
async def test_idempotency_key_advances_after_a_cancel_then_reissue():
    """A deliberate re-issue leaves the cancelled row behind, so the key must
    move on. Reusing it would make the provider replay the ORIGINAL (now
    closed) card inside its key-retention window — a dead card to the vendor."""
    inv = _invoice()

    first = await _capture_payload(inv, db=_db(existing_cards=0))
    reissue = await _capture_payload(inv, db=_db(existing_cards=1))

    assert first.idempotency_key != reissue.idempotency_key


def test_build_card_idempotency_key_is_pure_and_deterministic():
    """No clock, no randomness, no uuid4 — same inputs, same key, forever
    (including across processes and deploys)."""
    from app.services.card_issuance import build_card_idempotency_key

    inv_id = uuid.uuid4()
    corr = uuid.uuid4()

    k1 = build_card_idempotency_key(invoice_id=inv_id, correlation_id=corr, reissue_seq=0)
    k2 = build_card_idempotency_key(invoice_id=inv_id, correlation_id=corr, reissue_seq=0)

    assert k1 == k2
    assert uuid.UUID(k1)
    # Falls back to the invoice id for legacy rows with no correlation id, and
    # still produces a usable, distinct key.
    legacy = build_card_idempotency_key(invoice_id=inv_id, correlation_id=None, reissue_seq=0)
    assert uuid.UUID(legacy)
    assert legacy != k1
