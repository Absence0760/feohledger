"""`POST /api/cards/{id}/cancel` must not record an unverified cancel.

The fail-safe direction for a cancel is "dead at the provider, maybe stale in
the DB"; the DANGEROUS direction is a card the AP team believes is cancelled
while it is still chargeable at the provider. So the handler only marks the row
`cancelled` once the provider CONFIRMS the close:

  - provider raises (unreachable, state unknown) → 502, no DB change
  - provider returns False (didn't confirm)      → 502, no DB change
  - provider returns True                         → mark cancelled + commit
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


def _card_row():
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider_card_id="card_tok",
        status="created",
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        last_four="4242",
    )


def _fake_db(card):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=card)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def _org():
    return SimpleNamespace(
        id=uuid.uuid4(),
        settings={"cards": {"enabled": True, "program_type": "platform", "region": "US"}},
    )


def _user():
    return SimpleNamespace(id=uuid.uuid4(), roles=[SimpleNamespace(name="admin")])


async def _call_cancel(adapter, card, db):
    from app.api.cards import cancel_card

    with (
        patch("app.services.card_adapters.get_card_adapter", return_value=adapter),
        patch("app.services.audit_dispatch.dispatch_audit", new=AsyncMock()),
    ):
        return await cancel_card(card_id=card.id, db=db, org=_org(), user=_user(), entity_id=None)


@pytest.mark.asyncio
async def test_provider_raises_leaves_card_uncancelled_and_502():
    card = _card_row()
    db = _fake_db(card)
    adapter = MagicMock()
    adapter.cancel_card = AsyncMock(side_effect=RuntimeError("connect timeout"))

    with pytest.raises(HTTPException) as exc:
        await _call_cancel(adapter, card, db)

    assert exc.value.status_code == 502
    assert card.status == "created", "must not record a cancel the provider never confirmed"
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_provider_returns_false_leaves_card_uncancelled_and_502():
    card = _card_row()
    db = _fake_db(card)
    adapter = MagicMock()
    adapter.cancel_card = AsyncMock(return_value=False)

    with pytest.raises(HTTPException) as exc:
        await _call_cancel(adapter, card, db)

    assert exc.value.status_code == 502
    assert card.status == "created"
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_provider_confirms_close_marks_cancelled_and_commits():
    card = _card_row()
    db = _fake_db(card)
    adapter = MagicMock()
    adapter.cancel_card = AsyncMock(return_value=True)

    out = await _call_cancel(adapter, card, db)

    assert out == {"success": True, "message": "Card cancelled"}
    assert card.status == "cancelled"
    db.commit.assert_called()
