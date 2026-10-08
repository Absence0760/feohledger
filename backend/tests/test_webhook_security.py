"""Webhook security primitives + integration tests.

Project invariant #9: "Webhook handlers verify signatures and dedupe
by event id." The shared `services/webhook_security.py` is the home
for the HMAC + dedup helpers; this test file pins:

  - HMAC primitive uses constant-time compare + SHA-256
  - Dedup uses Redis SET-IF-NOT-EXISTS with TTL (replays caught
    inside the window, expired entries free up)
  - Card webhook handler refuses unsigned / wrong-sig requests
  - Card webhook deduplicates by event id across deliveries
  - ERP webhook handler refuses unsigned / wrong-sig requests
  - ERP webhook deduplicates by event id
  - Both webhooks return 204 silently on every rejection path
    (no enumeration of tenant slugs / card tokens)
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The fake org's settings carry the webhook secrets; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

# ---------------------------------------------------------------------------
# Primitive: verify_hmac_sha256
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_verify_hmac_accepts_correctly_signed_body():
    from app.services.webhook_security import verify_hmac_sha256

    body = b'{"event":"settled"}'
    sig = _sign("s3cret", body)
    assert verify_hmac_sha256("s3cret", body, sig) is True


def test_verify_hmac_rejects_wrong_signature():
    from app.services.webhook_security import verify_hmac_sha256

    body = b'{"event":"settled"}'
    # Off-by-one signature
    bad_sig = _sign("s3cret", body)[:-1] + ("A" if _sign("s3cret", body)[-1] != "A" else "B")
    assert verify_hmac_sha256("s3cret", body, bad_sig) is False


def test_verify_hmac_rejects_when_body_was_tampered():
    """If an attacker modifies the body after signing, the HMAC must
    fail — that's the whole point of signing the bytes-on-the-wire."""
    from app.services.webhook_security import verify_hmac_sha256

    sig = _sign("s3cret", b'{"event":"settled","amount":100}')
    # Same key, different body — sig won't match.
    assert verify_hmac_sha256("s3cret", b'{"event":"settled","amount":99999}', sig) is False


def test_verify_hmac_rejects_when_secret_is_empty():
    """An unconfigured secret must NEVER be the green light — without
    this check, "no secret = anyone can post" would be the default."""
    from app.services.webhook_security import verify_hmac_sha256

    body = b'{"x":1}'
    sig = _sign("anything", body)
    assert verify_hmac_sha256("", body, sig) is False


def test_verify_hmac_rejects_when_signature_is_missing():
    from app.services.webhook_security import verify_hmac_sha256

    assert verify_hmac_sha256("s3cret", b'{"x":1}', None) is False
    assert verify_hmac_sha256("s3cret", b'{"x":1}', "") is False


def test_verify_hmac_uses_constant_time_compare():
    """`hmac.compare_digest` is the documented constant-time
    comparator. A regression that swapped it for `==` would leak
    the signature one character at a time via response timing."""
    import inspect

    from app.services import webhook_security

    src = inspect.getsource(webhook_security.verify_hmac_sha256)
    assert "compare_digest" in src, "verify_hmac_sha256 must use hmac.compare_digest"


# ---------------------------------------------------------------------------
# Primitive: extract_signature_header
# ---------------------------------------------------------------------------


def test_extract_signature_header_is_case_insensitive():
    """HTTP headers are case-insensitive. The helper must find the
    signature regardless of how the provider cased its header."""
    from app.services.webhook_security import extract_signature_header

    headers = {"webhook-signature": "sig123"}
    assert extract_signature_header(headers, "Webhook-Signature") == "sig123"

    headers = {"WEBHOOK-SIGNATURE": "sig456"}
    assert extract_signature_header(headers, "Webhook-Signature") == "sig456"


def test_extract_signature_header_picks_first_present_candidate():
    """Some providers ship under multiple legacy names. The helper
    walks candidates in order — first hit wins."""
    from app.services.webhook_security import extract_signature_header

    headers = {"x-signature": "v1", "webhook-signature": "v2"}
    assert extract_signature_header(headers, "Webhook-Signature", "X-Signature") == "v2"


def test_extract_signature_header_returns_none_when_no_match():
    from app.services.webhook_security import extract_signature_header

    assert extract_signature_header({}, "Webhook-Signature") is None
    assert extract_signature_header({"other": "x"}, "Webhook-Signature") is None


# ---------------------------------------------------------------------------
# Primitive: is_event_already_processed (Redis dedup)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, tuple[float | None, str]] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None  # already set
        expiry = time.time() + ex if ex else None
        self.store[key] = (expiry, value)
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    return fake


@pytest.mark.asyncio
async def test_dedup_first_call_returns_false_subsequent_returns_true(fake_redis):
    """The semantics: first delivery returns False ("you're first,
    process it"); every retry within TTL returns True ("already done,
    no-op"). A regression that flipped the polarity would cause every
    event to be ignored OR every retry to be re-processed."""
    from app.services.webhook_security import is_event_already_processed

    assert await is_event_already_processed("lithic", "evt_1") is False
    assert await is_event_already_processed("lithic", "evt_1") is True
    assert await is_event_already_processed("lithic", "evt_1") is True


@pytest.mark.asyncio
async def test_dedup_scopes_by_provider(fake_redis):
    """Different providers shouldn't collide. Lithic's `evt_42`
    and Nium's `evt_42` are unrelated events."""
    from app.services.webhook_security import is_event_already_processed

    assert await is_event_already_processed("lithic", "evt_42") is False
    # Same event id, different provider → independent bucket.
    assert await is_event_already_processed("nium", "evt_42") is False


@pytest.mark.asyncio
async def test_event_claim_exists_reads_without_claiming():
    """Read-only probe for a live claim (uses the conftest Redis stub). It must
    never claim: probing twice and then claiming still finds the event new."""
    from app.services.webhook_security import (
        event_claim_exists,
        is_event_already_processed,
        release_event_claim,
    )

    assert await event_claim_exists("lithic", "evt_probe") is False
    assert await event_claim_exists("lithic", "evt_probe") is False
    assert await is_event_already_processed("lithic", "evt_probe") is False
    assert await event_claim_exists("lithic", "evt_probe") is True
    assert await event_claim_exists("nium", "evt_probe") is False
    await release_event_claim("lithic", "evt_probe")
    assert await event_claim_exists("lithic", "evt_probe") is False
    assert await event_claim_exists("lithic", "") is False


@pytest.mark.asyncio
async def test_dedup_returns_false_when_event_id_is_empty(fake_redis):
    """An empty event id can't be deduped — every "no id" delivery
    must be treated as first-time. Logging happens but the helper
    doesn't fail closed (provider quirks shouldn't blackhole events)."""
    from app.services.webhook_security import is_event_already_processed

    assert await is_event_already_processed("lithic", "") is False
    assert await is_event_already_processed("lithic", None) is False


# ---------------------------------------------------------------------------
# Card webhook integration
# ---------------------------------------------------------------------------


def _fake_request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)

    async def _json():
        import json

        return json.loads(body.decode("utf-8"))

    req.json = _json
    req.headers = headers or {}
    return req


def _fake_ctrl_session_factory(orgs):
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=orgs)
    result.scalars = MagicMock(return_value=scalars)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _fake_tenant_session_factory(card):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=card)
    db = AsyncMock()
    # SQLAlchemy's Session.add is synchronous — override to a plain
    # MagicMock so the handler's `db.add(rebate)` call doesn't leak
    # an unawaited-coroutine RuntimeWarning.
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    # Session.begin_nested() is synchronous and returns an async context
    # manager (the savepoint). A plain MagicMock supports the async-CM
    # protocol; leaving it as an AsyncMock child would return a coroutine
    # and blow up `async with db.begin_nested():` — the rebate savepoint the
    # settled-event path opens for the one-rebate-per-card backstop.
    db.begin_nested = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, db


def _org_with_card_secret(secret: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"cards": {"webhook_signing_secret": secret}},
    )


def _card_row(provider_card_id: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider_card_id=provider_card_id,
        status="created",
        amount_limit=100,
        amount_charged=None,
        charged_at=None,
        merchant_name=None,
        organization_id=uuid.uuid4(),
        # The webhook writes a `card.charged`/`card.settled` audit row that reads
        # these fields — the fake card must provide them, or the handler's audit
        # call raises and rolls back instead of committing.
        correlation_id=uuid.uuid4(),
        last_four="4242",
        # `VirtualCard.currency` is a non-nullable column with a "USD" default,
        # so a real row ALWAYS has one. The webhook reads it to pick the
        # ISO-4217 minor-unit exponent; without it the handler raises
        # AttributeError, which the broad `except` turns into a silent 204 —
        # the mutation is applied in memory but never committed, so the test
        # sees a "charged" card and no commit.
        currency="USD",
    )


@pytest.mark.asyncio
async def test_card_webhook_rejects_unsigned_request_silently(fake_redis):
    """An unsigned POST that names a real card_token must NOT cause a
    state change. The handler returns 204 either way; pin that the
    DB commit did not run."""
    import json

    from app.api.cards import card_webhook

    secret = "card-s3cret"
    org = _org_with_card_secret(secret)
    card = _card_row("card_token_abc")

    body_bytes = json.dumps(
        {"card_token": "card_token_abc", "type": "authorization", "amount": 5000, "event_id": "e1"}
    ).encode("utf-8")

    tenant_factory, db = _fake_tenant_session_factory(card)

    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        # No signature header at all
        result = await card_webhook(provider="lithic", request=_fake_request(body_bytes))

    assert result is None  # silent 204
    db.commit.assert_not_called()
    assert card.status == "created", "card status must not change without HMAC verification"


@pytest.mark.asyncio
async def test_card_webhook_rejects_wrong_signature_silently(fake_redis):
    """A signature minted with the wrong secret must be refused —
    the verification step is the only thing standing between a
    public webhook URL and a rebate-mint vector."""
    import json

    from app.api.cards import card_webhook

    org = _org_with_card_secret("real-secret")
    card = _card_row("card_token_abc")

    body_bytes = json.dumps(
        {"card_token": "card_token_abc", "type": "authorization", "amount": 5000, "event_id": "e2"}
    ).encode("utf-8")
    wrong_sig = _sign("attacker-guess", body_bytes)

    tenant_factory, db = _fake_tenant_session_factory(card)

    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        result = await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, {"Webhook-Signature": wrong_sig}),
        )

    assert result is None
    db.commit.assert_not_called()
    assert card.status == "created"


@pytest.mark.asyncio
async def test_card_webhook_dedups_replayed_event(fake_redis):
    """A correctly-signed event that's already been processed must
    NOT re-trigger the rebate flow on the second delivery. Without
    this, a settled-event retry creates a duplicate rebate row."""
    import json

    from app.api.cards import card_webhook

    secret = "real-secret"
    org = _org_with_card_secret(secret)

    body_bytes = json.dumps(
        {
            "card_token": "card_token_abc",
            "type": "transaction.settled",
            "amount": 5000,
            "event_id": "evt_dup_check",
        }
    ).encode("utf-8")
    sig = _sign(secret, body_bytes)

    # First delivery — card should advance
    card_first = _card_row("card_token_abc")
    card_first.status = "charged"  # settling assumes the auth already landed
    card_first.amount_charged = 5000
    tenant_factory_1, db_1 = _fake_tenant_session_factory(card_first)
    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory_1),
    ):
        await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, {"Webhook-Signature": sig}),
        )
    assert card_first.status == "completed"  # advanced
    db_1.commit.assert_called()

    # Second delivery of the SAME event — must short-circuit before
    # touching state, even though the signature is correct.
    card_second = _card_row("card_token_abc")
    card_second.status = "charged"
    card_second.amount_charged = 5000
    tenant_factory_2, db_2 = _fake_tenant_session_factory(card_second)
    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory_2),
    ):
        await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, {"Webhook-Signature": sig}),
        )
    assert card_second.status == "charged", (
        "second delivery of the same event must not advance the card"
    )
    db_2.commit.assert_not_called()


@pytest.mark.asyncio
async def test_card_webhook_accepts_correctly_signed_first_delivery(fake_redis):
    """Positive control — confirm the happy path still works once
    HMAC + dedup are in place. Without it, the negatives could pass
    because every request is rejected."""
    import json

    from app.api.cards import card_webhook

    secret = "real-secret"
    org = _org_with_card_secret(secret)
    card = _card_row("card_token_abc")

    body_bytes = json.dumps(
        {
            "card_token": "card_token_abc",
            "type": "authorization",
            "amount": 5000,
            "event_id": "evt_ok",
        }
    ).encode("utf-8")
    sig = _sign(secret, body_bytes)

    tenant_factory, db = _fake_tenant_session_factory(card)
    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        result = await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, {"Webhook-Signature": sig}),
        )

    assert result is None
    assert card.status == "charged"
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_card_webhook_releases_dedup_claim_when_commit_fails(fake_redis):
    """The lost-event guard: if the DB commit rolls back AFTER the Redis
    dedup claim, the claim must be RELEASED so the provider's retry can
    reprocess. Otherwise the event is stranded as "processed" for the full
    TTL and the rebate / charge is dropped forever."""
    import json

    from app.api.cards import card_webhook
    from app.services.webhook_security import is_event_already_processed

    secret = "real-secret"
    org = _org_with_card_secret(secret)
    card = _card_row("card_token_abc")

    body_bytes = json.dumps(
        {
            "card_token": "card_token_abc",
            "type": "authorization",
            "amount": 5000,
            "event_id": "evt_commit_fail",
        }
    ).encode("utf-8")
    sig = _sign(secret, body_bytes)

    tenant_factory, db = _fake_tenant_session_factory(card)
    # The commit fails (deadlock / pool exhaustion) — the handler must roll
    # back AND release the dedup claim.
    db.commit = AsyncMock(side_effect=RuntimeError("db overloaded"))

    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        result = await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, {"Webhook-Signature": sig}),
        )

    assert result is None  # still a silent 204
    db.rollback.assert_called()
    # The claim was released → a retry is NOT deduped (returns False = "first").
    assert await is_event_already_processed("lithic", "evt_commit_fail") is False


@pytest.mark.asyncio
async def test_card_webhook_rejects_when_secret_unconfigured(fake_redis):
    """An org that hasn't configured a webhook secret must NOT accept
    any webhook — even an "unsigned" one. Otherwise activating cards
    for a tenant before setting the secret silently makes that
    tenant's rebate flow forgeable."""
    import json

    from app.api.cards import card_webhook

    org = _org_with_card_secret("")  # blank
    card = _card_row("card_token_abc")

    body_bytes = json.dumps(
        {"card_token": "card_token_abc", "type": "authorization", "amount": 1, "event_id": "e3"}
    ).encode("utf-8")

    tenant_factory, db = _fake_tenant_session_factory(card)
    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, {"Webhook-Signature": _sign("anything", body_bytes)}),
        )

    db.commit.assert_not_called()
    assert card.status == "created"


# ---------------------------------------------------------------------------
# ERP webhook integration
# ---------------------------------------------------------------------------


def _ctrl_session_for_org(org):
    """Mock the control-plane session whose first execute() returns
    `org` (or None for the unknown-tenant path)."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _org_with_erp_secret(secret: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"erp": {"webhook_signing_secret": secret}},
    )


@pytest.mark.asyncio
async def test_erp_webhook_rejects_unsigned_request_silently(fake_redis):
    """ERP status webhooks can transition invoices to `paid` — a
    forged event without HMAC would let an attacker mark invoices
    paid in a tenant's books. Must reject silently."""
    import json

    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("erp-secret")
    body_bytes = json.dumps(
        {
            "tenant_slug": "acme",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_1",
            "status": "Paid",
            "event_id": "ev_1",
        }
    ).encode("utf-8")

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine") as mk_engine,
        patch("app.api.erp_webhook.async_sessionmaker") as mk_factory,
    ):
        result = await erp_webhook(
            erp_type="merge_dev",
            request=_fake_request(body_bytes),
        )

    assert result is None
    # No tenant DB session opened on the rejection path.
    mk_engine.assert_not_called()
    mk_factory.assert_not_called()


@pytest.mark.asyncio
async def test_erp_webhook_rejects_wrong_signature_silently(fake_redis):
    import json

    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("real")
    body_bytes = json.dumps(
        {
            "tenant_slug": "acme",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_2",
            "status": "Paid",
            "event_id": "ev_2",
        }
    ).encode("utf-8")
    wrong_sig = _sign("attacker", body_bytes)

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine") as mk_engine,
    ):
        await erp_webhook(
            erp_type="merge_dev",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": wrong_sig}),
        )

    mk_engine.assert_not_called()


@pytest.mark.asyncio
async def test_erp_webhook_silently_returns_for_unknown_tenant(fake_redis):
    """A body that names a tenant slug we've never heard of must
    NOT distinguish itself from a bad-signature response — both 204
    silently so an attacker can't enumerate slugs."""
    import json

    from app.api.erp_webhook import erp_webhook

    body_bytes = json.dumps(
        {
            "tenant_slug": "no-such-tenant",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_3",
            "status": "Paid",
            "event_id": "ev_3",
        }
    ).encode("utf-8")

    with patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(None)):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": "sig"}),
        )

    assert result is None


@pytest.mark.asyncio
async def test_erp_webhook_whitelists_audit_details_no_pii(fake_redis):
    """A signed ERP webhook whose `details` block carries vendor PII (bank
    account / tax id / address) must NOT splat that into the append-only audit
    row. Only the non-PII routing identifiers are recorded."""
    import json

    from app.api.erp_webhook import erp_webhook
    from app.models.invoice import InvoiceStatus

    secret = "erp-secret"
    org = _org_with_erp_secret(secret)
    body = {
        "tenant_slug": "acme",
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_9",
        "status": "Open",  # → posted_in_erp (a legal step from sent_to_erp)
        "event_id": "ev_9",
        "details": {
            "vendor_bank_account": "123456789",
            "vendor_tin": "12-3456789",
            "vendor_address": "1 Secret Lane",
        },
    }
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign(secret, body_bytes)

    invoice = SimpleNamespace(status=InvoiceStatus.sent_to_erp)
    tenant_factory, db = _fake_tenant_session_factory(invoice)

    captured: dict = {}

    async def _capture(_db, _inv, _target, *, action_name, details):
        captured["details"] = details

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch("app.api.erp_webhook.async_sessionmaker", return_value=tenant_factory),
        patch("app.api.erp_webhook.transition_invoice", _capture),
    ):
        await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": sig}),
        )

    details = captured["details"]
    assert set(details) == {"erp_type", "erp_status", "erp_document_id", "raw_event_id"}
    # None of the ERP payload's PII leaked into the audit row.
    serialized = json.dumps(details)
    assert "123456789" not in serialized
    assert "12-3456789" not in serialized
    assert "Secret Lane" not in serialized


@pytest.mark.asyncio
async def test_erp_webhook_logs_when_signing_secret_unconfigured(fake_redis, caplog):
    """An org that never set an ERP webhook signing secret must fail closed
    (silent 204, no tenant DB touched) AND surface a PII-free config warning so
    operators learn the integration is unconfigured rather than silently
    dropping every event. The secret value is never logged (it's empty here)."""
    import json
    import logging

    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("")  # unconfigured
    body_bytes = json.dumps(
        {
            "tenant_slug": "acme",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_x",
            "status": "Paid",
            "event_id": "ev_x",
        }
    ).encode("utf-8")

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine") as mk_engine,
        caplog.at_level(logging.WARNING, logger="app.api.erp_webhook"),
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": _sign("x", body_bytes)}),
        )

    assert result is None  # silent 204 to the caller
    mk_engine.assert_not_called()  # never opened the tenant DB
    assert any(
        "signing_secret" in r.getMessage() and "acme" in r.getMessage() for r in caplog.records
    ), "expected a PII-free unconfigured-secret warning"
