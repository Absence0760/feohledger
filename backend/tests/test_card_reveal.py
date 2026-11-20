"""Tests for the card-reveal single-use-token service.

Pure async service tests — no DB. The session is replaced with an
AsyncMock so we can assert the right rows are added / mutated without a
real Postgres connection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _card(**overrides):
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        last_four="1234",
        card_provider="mock",
        provider_card_id="mock_card_xyz",
        amount_limit=100,
        currency="USD",
        correlation_id=uuid.uuid4(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _result_for(scalar=None, row=None):
    """Build the SQLAlchemy result object .execute() should return.

    ``scalar`` feeds ``scalar_one_or_none`` (the atomic claim's RETURNING
    card_id, and the card lookup); ``row`` feeds ``one_or_none`` (the
    ``(expires_at, used_at)`` tuple the failure classifier reads).
    """
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar)
    r.one_or_none = MagicMock(return_value=row)
    return r


def _sql(db, index: int) -> str:
    """Compiled SQL of the index-th statement passed to db.execute."""
    from sqlalchemy.dialects import postgresql

    stmt = db.execute.await_args_list[index].args[0]
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_mint_token_persists_hashed_token_and_returns_plaintext():
    from app.services.card_reveal import _hash, mint_reveal_token

    db = AsyncMock()
    db.add = MagicMock()

    card = _card()
    plaintext = await mint_reveal_token(db, card)

    assert isinstance(plaintext, str)
    assert len(plaintext) >= 32  # secrets.token_urlsafe(32) → ~43 chars

    persisted = db.add.call_args.args[0]
    # The hash on the row matches the plaintext we returned.
    assert persisted.token_hash == _hash(plaintext)
    # Card + org are scoped.
    assert persisted.card_id == card.id
    assert persisted.organization_id == card.organization_id
    # Default expiry is 7 days from now.
    delta = persisted.expires_at - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


@pytest.mark.asyncio
async def test_consume_returns_invalid_for_unknown_token():
    from app.services.card_reveal import consume_reveal_token

    db = AsyncMock()
    # Claim matches nothing; the classifier finds no row at all.
    db.execute = AsyncMock(side_effect=[_result_for(scalar=None), _result_for(row=None)])

    card, error = await consume_reveal_token(db, "nope")
    assert card is None
    assert error == "invalid"


@pytest.mark.asyncio
async def test_consume_returns_expired_when_past_expires_at():
    from app.services.card_reveal import consume_reveal_token

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _result_for(scalar=None),
            _result_for(row=(datetime.now(UTC) - timedelta(seconds=1), None)),
        ]
    )

    card, error = await consume_reveal_token(db, "anything")
    assert card is None
    assert error == "expired"


@pytest.mark.asyncio
async def test_consume_returns_used_when_already_consumed():
    from app.services.card_reveal import consume_reveal_token

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _result_for(scalar=None),
            _result_for(
                row=(datetime.now(UTC) + timedelta(days=1), datetime.now(UTC) - timedelta(hours=1))
            ),
        ]
    )

    card, error = await consume_reveal_token(db, "anything")
    assert card is None
    assert error == "used"


@pytest.mark.asyncio
async def test_consume_claims_atomically_and_returns_the_card():
    """The claim is ONE `UPDATE … WHERE used_at IS NULL … RETURNING card_id`,
    not a read-then-write pair — that single statement is what makes two
    simultaneous reveals of the same token impossible."""
    from app.services.card_reveal import consume_reveal_token

    card_obj = _card()
    db = AsyncMock()
    # First execute: the atomic claim (RETURNING card_id). Second: card lookup.
    db.execute = AsyncMock(
        side_effect=[_result_for(scalar=card_obj.id), _result_for(scalar=card_obj)]
    )

    card, error = await consume_reveal_token(db, "anything")
    assert error is None
    assert card is card_obj

    claim_sql = _sql(db, 0)
    assert claim_sql.startswith("UPDATE card_reveal_tokens SET used_at=")
    # The single-use guard lives in the UPDATE's own predicate, evaluated by
    # Postgres under the row lock — never in Python after a plain SELECT.
    assert "card_reveal_tokens.used_at IS NULL" in claim_sql
    assert "card_reveal_tokens.expires_at >" in claim_sql
    assert "RETURNING card_reveal_tokens.card_id" in claim_sql


@pytest.mark.asyncio
async def test_claim_binds_the_card_org_inside_the_update_predicate():
    """The defense-in-depth card/org cross-check is part of the claim's WHERE
    (an EXISTS on virtual_cards), so a mismatched card means the UPDATE matches
    no row and `used_at` is never stamped — nothing burns on a rejected reveal.
    (Behaviour is asserted end-to-end against Postgres further down.)"""
    from app.services.card_reveal import consume_reveal_token

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result_for(scalar=None), _result_for(row=None)])

    card, error = await consume_reveal_token(db, "anything", organization_id=uuid.uuid4())
    assert card is None
    assert error == "invalid"

    claim_sql = _sql(db, 0)
    assert "EXISTS (SELECT virtual_cards.id" in claim_sql
    assert "virtual_cards.id = card_reveal_tokens.card_id" in claim_sql
    assert "virtual_cards.organization_id" in claim_sql
    assert "card_reveal_tokens.organization_id" in claim_sql


@pytest.mark.asyncio
async def test_consume_scopes_token_lookup_to_the_org(realdb):
    """A valid token consumed with the WRONG organization_id must not resolve —
    the token-row query itself is org-scoped, so a token can't be pivoted onto
    another tenant even if the (impossible) wrong DB were queried."""
    from app.services.card_reveal import consume_reveal_token

    mk = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    _card_id, token = await _seed_card_with_token(mk, org_a)

    # Wrong org id → invalid, and the token stays consumable.
    async with mk() as s:
        card_bad, error_bad = await consume_reveal_token(s, token, organization_id=uuid.uuid4())
        await s.commit()
    assert card_bad is None
    assert error_bad == "invalid"

    # The correct org id reveals it (proving the guard isn't failing-closed
    # for the wrong reason).
    async with mk() as s:
        card_ok, error_ok = await consume_reveal_token(s, token, organization_id=org_a)
        await s.commit()
    assert error_ok is None
    assert card_ok is not None


@pytest.mark.asyncio
async def test_reveal_token_not_burned_when_session_rolled_back(realdb):
    """Rolling the claiming transaction back releases the claim: `used_at` was
    never committed, so the link is consumable again.

    This is why `api/portal.py::reveal_card` COMMITS the claim before it calls
    the card provider — an uncommitted claim is only as durable as the request
    that holds it, and a commit that fails after the PAN has gone out on the
    wire would silently revive a link that was already revealed."""
    from app.services.card_reveal import consume_reveal_token

    mk = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    card_id, token = await _seed_card_with_token(mk, org_a)

    # First attempt: consume marks used_at, but we roll back (mimics the outage
    # branch that returns the fallback body without committing).
    async with mk() as s:
        card, error = await consume_reveal_token(s, token, organization_id=org_a)
        assert error is None
        await s.rollback()

    # Retry after "provider recovery": the token is still alive.
    async with mk() as s:
        card2, error2 = await consume_reveal_token(s, token, organization_id=org_a)
        await s.commit()
    assert error2 is None
    assert card2 is not None
    assert card2.id == card_id


# ---------------------------------------------------------------------------
# Real-Postgres: single-use survives a real commit + re-read, and the token
# is scoped to its own tenant DB (a token minted in A can't reveal in B).
# ---------------------------------------------------------------------------


async def _seed_card_with_token(mk, org_id):
    """Create an invoice + virtual card in a tenant DB and mint a reveal
    token, committing. Returns (card_id, plaintext_token)."""
    from decimal import Decimal

    from app.models.invoice import Invoice
    from app.models.virtual_card import VirtualCard
    from app.services.card_reveal import mint_reveal_token

    async with mk() as s:
        invoice = Invoice(
            organization_id=org_id,
            invoice_number="INV-CARD-1",
            vendor_name="Acme",
            amount=Decimal("100.00"),
            currency="USD",
        )
        s.add(invoice)
        await s.flush()
        card = VirtualCard(
            organization_id=org_id,
            invoice_id=invoice.id,
            card_provider="mock",
            provider_card_id="mock_card_x",
            amount_limit=Decimal("100.00"),
            last_four="4321",
        )
        s.add(card)
        await s.flush()
        token = await mint_reveal_token(s, card)
        card_id = card.id
        await s.commit()
    return card_id, token


@pytest.mark.asyncio
async def test_reveal_token_single_use_survives_commit(realdb):
    from app.services.card_reveal import consume_reveal_token

    mk = realdb.sessionmaker("a")
    card_id, token = await _seed_card_with_token(mk, realdb.info("a").org_id)

    # First reveal: returns the card and flips used_at — committed.
    async with mk() as s:
        card, error = await consume_reveal_token(s, token)
        await s.commit()
    assert error is None
    assert card.id == card_id

    # Second reveal in a fresh session: the committed used_at makes it dead.
    async with mk() as s:
        card2, error2 = await consume_reveal_token(s, token)
        await s.commit()
    assert card2 is None
    assert error2 == "used"


@pytest.mark.asyncio
async def test_concurrent_reveals_claim_the_token_exactly_once(realdb):
    """THE single-use guarantee, under concurrency.

    Four simultaneous reveals of the same plaintext token, each on its own
    connection. Exactly one may come back with the card (and therefore go on to
    fetch the live PAN/CVV); the rest must be told the link is spent. A plain
    `SELECT` + Python-side `used_at is None` check lets every one of them
    through — they all read before any of them writes.
    """
    import asyncio

    from app.services.card_reveal import consume_reveal_token

    mk = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    card_id, token = await _seed_card_with_token(mk, org_a)

    async def attempt():
        async with mk() as s:
            card, error = await consume_reveal_token(s, token, organization_id=org_a)
            # Commit whatever the claim decided — a winner burns the token, a
            # loser committing an empty transaction changes nothing.
            await s.commit()
            return (card.id if card is not None else None), error

    results = await asyncio.gather(*(attempt() for _ in range(4)))

    winners = [r for r in results if r[1] is None]
    assert len(winners) == 1, f"expected exactly one reveal, got {results}"
    assert winners[0][0] == card_id
    # Every loser is told the link is spent — never handed the card.
    assert [r[1] for r in results if r[1] is not None] == ["used"] * 3
    assert all(r[0] is None for r in results if r[1] is not None)


@pytest.mark.asyncio
async def test_consume_refuses_a_card_in_another_org_without_burning(realdb):
    """Defense-in-depth org binding, end-to-end: the token row carries the
    caller's org but the card it points at carries a different one. The reveal
    is refused as the opaque `invalid`, and — because the card cross-check is
    part of the claim's own WHERE — `used_at` is never stamped."""
    from decimal import Decimal

    from sqlalchemy import select, update

    from app.models.invoice import Invoice
    from app.models.virtual_card import CardRevealToken, VirtualCard
    from app.services.card_reveal import _hash, consume_reveal_token, mint_reveal_token

    mk = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    other_org = uuid.uuid4()

    async with mk() as s:
        invoice = Invoice(
            organization_id=org_a,
            invoice_number="INV-CARD-XORG",
            vendor_name="Acme",
            amount=Decimal("100.00"),
            currency="USD",
        )
        s.add(invoice)
        await s.flush()
        # Card stamped with a DIFFERENT org than the token below.
        card = VirtualCard(
            organization_id=other_org,
            invoice_id=invoice.id,
            card_provider="mock",
            provider_card_id="mock_card_xorg",
            amount_limit=Decimal("100.00"),
            last_four="9999",
        )
        s.add(card)
        await s.flush()
        token = await mint_reveal_token(s, card)
        await s.flush()
        # Token row belongs to the caller's org; the card it points at does not.
        await s.execute(
            update(CardRevealToken)
            .where(CardRevealToken.token_hash == _hash(token))
            .values(organization_id=org_a)
        )
        await s.commit()

    async with mk() as s:
        card_out, error = await consume_reveal_token(s, token, organization_id=org_a)
        await s.commit()
    assert card_out is None
    assert error == "invalid"

    async with mk() as s:
        used_at = (
            await s.execute(
                select(CardRevealToken.used_at).where(CardRevealToken.token_hash == _hash(token))
            )
        ).scalar_one()
    assert used_at is None, "a rejected cross-org reveal must not burn the token"


@pytest.mark.asyncio
async def test_reveal_token_is_scoped_to_its_tenant_db(realdb):
    """A token minted in tenant A's DB must not resolve against tenant B —
    the token_hash lookup runs on the per-tenant session, so cross-tenant
    PAN reveal is impossible."""
    from app.services.card_reveal import consume_reveal_token

    mk_a = realdb.sessionmaker("a")
    mk_b = realdb.sessionmaker("b")
    card_id, token = await _seed_card_with_token(mk_a, realdb.info("a").org_id)

    # Tenant B cannot see the token at all.
    async with mk_b() as s:
        card_b, error_b = await consume_reveal_token(s, token)
    assert card_b is None
    assert error_b == "invalid"

    # Tenant A (the owner) reveals it fine.
    async with mk_a() as s:
        card_a, error_a = await consume_reveal_token(s, token)
        await s.commit()
    assert error_a is None
    assert card_a.id == card_id
