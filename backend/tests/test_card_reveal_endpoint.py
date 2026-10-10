"""HTTP-boundary tests for the single-use card-reveal endpoint.

``test_card_reveal.py`` covers the service layer (mint/consume branches)
against a mocked session. This file covers the ``GET /portal/cards/{token}``
*handler* — the glue the service tests never reach:

  * consume-reason -> HTTP-status mapping (invalid 404, expired 410, used 410)
  * the append-only audit row written when the single-use token is consumed
    (``card.revealed_via_token``, actor_id=None) — a regression that drops
    the dispatch would otherwise be invisible
  * **the claim is committed BEFORE the outbound provider call** — the
    ordering that keeps a slow / failing / crashing provider round-trip from
    leaving a link that has already been revealed re-usable
  * PII suppression on the two degraded paths (org disabled cards after
    issuance; adapter outage) — pan/cvv must come back None and no PAN leaks;
    the token stays spent on both (fail-closed by design: a link that survives
    a failed reveal is indistinguishable from a twice-revealable link)
  * the resolved tenant's org id is threaded into consume_reveal_token as the
    defense-in-depth binding (token + card must both belong to the tenant)

The handler imports its collaborators inside the function from their source
modules, so we patch them at the source. The tenant ``db`` is mocked and the
tenant ``Organization`` is passed in directly (reveal_card no longer opens a
control-plane session) — matching the suite's unit-test convention.

Out of scope here (covered elsewhere, by design):
  * single-use durability across a real commit+re-read — the ``used_at``
    flip is asserted in ``test_card_reveal.py`` against the service.
  * cross-tenant token reveal — the token lookup runs on the tenant ``db``
    resolved by ``get_tenant_db``, whose isolation chokepoint is tested in
    ``test_tenant_isolation.py``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.portal import reveal_card

# Fake orgs carry their provider secrets inline; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")


def _card(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        last_four="4321",
        amount_limit=Decimal("100.00"),
        currency="USD",
        expires_at=None,
        provider_card_id="mock_card_xyz",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@contextmanager
def _patched(
    *,
    consume_return,
    config=None,
    details=None,
    adapter_raises=False,
):
    """Patch every collaborator reveal_card imports inside its body.

    Every side-effecting collaborator appends to a shared ``calls`` log so tests
    can assert the *order* of the claim-commit vs the provider round-trip, not
    just that both happened.
    """
    calls: list[str] = []

    # Build the tenant session explicitly (only execute/commit/rollback are
    # used) so a bare AsyncMock doesn't auto-spawn unawaited child coroutines at
    # GC time.
    db = MagicMock()
    vendor_result = MagicMock()
    vendor_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=vendor_result)
    db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    db.rollback = AsyncMock(side_effect=lambda: calls.append("rollback"))

    dispatch_audit = AsyncMock(side_effect=lambda *a, **kw: calls.append("audit"))
    # The resolved tenant Organization — reveal_card reads `tenant.settings`
    # directly (no control-plane session) and passes `tenant.id` to
    # consume_reveal_token as the defense-in-depth org binding.
    tenant = SimpleNamespace(id=uuid.uuid4(), settings={"cards": {"enabled": True}})

    def _provider(_card_id):
        calls.append("provider")
        if adapter_raises:
            raise RuntimeError("provider down")
        return details

    adapter = MagicMock()
    adapter.get_card_details = AsyncMock(side_effect=_provider)

    consume = AsyncMock(side_effect=lambda *a, **kw: (calls.append("consume"), consume_return)[1])
    with (
        patch("app.services.card_reveal.consume_reveal_token", consume),
        patch("app.services.card_issuance._resolve_card_config", MagicMock(return_value=config)),
        patch("app.services.card_adapters.get_card_adapter", MagicMock(return_value=adapter)),
        patch("app.services.audit_dispatch.dispatch_audit", dispatch_audit),
    ):
        yield SimpleNamespace(
            db=db,
            tenant=tenant,
            dispatch_audit=dispatch_audit,
            adapter=adapter,
            consume=consume,
            calls=calls,
        )


# ---------------------------------------------------------------------------
# consume-reason -> HTTP-status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "status"),
    [("invalid", 404), ("expired", 410), ("used", 410)],
)
async def test_reveal_maps_consume_reason_to_status(reason, status):
    with _patched(consume_return=(None, reason)) as h:
        with pytest.raises(HTTPException) as exc:
            await reveal_card(token="tok", tenant=h.tenant, db=h.db)
    assert exc.value.status_code == status
    # No PAN reveal and no audit row on any error path.
    h.dispatch_audit.assert_not_awaited()
    h.db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# success — PAN returned once + audit row written
# ---------------------------------------------------------------------------


async def test_reveal_success_returns_pan_and_writes_audit():
    card = _card()
    # Use the REAL CardDetails dataclass, not a hand-shaped stub — the handler
    # reads `details.card_number`, and a SimpleNamespace(pan=...) stub silently
    # masked an AttributeError that broke the entire success path in production.
    from app.services.card_adapters.base import CardDetails

    details = CardDetails(
        card_number="4111111111114321",
        exp_month=12,
        exp_year=2030,
        cvv="123",
        last_four="4321",
    )
    with _patched(
        consume_return=(card, None),
        config={"provider": "mock"},
        details=details,
    ) as h:
        body = await reveal_card(token="tok", tenant=h.tenant, db=h.db)

    assert body["pan"] == "4111111111114321"
    assert body["cvv"] == "123"
    assert body["last_four"] == "4321"
    assert body["amount_limit"] == 100.0

    h.dispatch_audit.assert_awaited_once()
    kwargs = h.dispatch_audit.await_args.kwargs
    assert kwargs["action"] == "card.revealed_via_token"
    assert kwargs["entity_type"] == "virtual_card"
    assert kwargs["entity_id"] == card.id
    assert kwargs["actor_id"] is None
    assert kwargs["organization_id"] == card.organization_id
    h.db.commit.assert_awaited_once()
    # The tenant's org id is threaded into consume as the defense-in-depth bind.
    assert h.consume.await_args.kwargs["organization_id"] == h.tenant.id
    # The claim (+ its audit row) is DURABLE before the provider is called: a
    # provider round-trip that hangs, fails, or takes the process down with it
    # can no longer leave a token that has already been revealed re-usable.
    assert h.calls == ["consume", "audit", "commit", "provider"]


async def test_reveal_commits_the_claim_before_calling_the_provider():
    """Ordering pin, stated on its own so it can't be diluted into the
    success-path assertions: the single-use claim must be committed BEFORE the
    outbound provider call, never after."""
    from app.services.card_adapters.base import CardDetails

    details = CardDetails(
        card_number="4111111111114321", exp_month=12, exp_year=2030, cvv="123", last_four="4321"
    )
    with _patched(
        consume_return=(_card(), None), config={"provider": "mock"}, details=details
    ) as h:
        await reveal_card(token="tok", tenant=h.tenant, db=h.db)

    assert h.calls.index("commit") < h.calls.index("provider")
    # And the claim is never handed back afterwards.
    h.db.rollback.assert_not_awaited()


# ---------------------------------------------------------------------------
# degraded paths — PII suppressed, token stays spent, no PAN leak
# ---------------------------------------------------------------------------


async def test_reveal_suppresses_pan_when_cards_disabled():
    """Org turned cards off after issuance (config resolves to None)."""
    card = _card()
    with _patched(consume_return=(card, None), config=None) as h:
        body = await reveal_card(token="tok", tenant=h.tenant, db=h.db)

    assert body["pan"] is None
    assert body["cvv"] is None
    assert body["warning"]
    assert body["last_four"] == "4321"
    # The claim was already committed, and stays committed: we never un-burn a
    # consumed link. The vendor asks AP for a new one.
    h.db.commit.assert_awaited_once()
    h.db.rollback.assert_not_awaited()
    # No provider call at all on this path.
    assert "provider" not in h.calls


async def test_reveal_suppresses_pan_on_adapter_outage():
    card = _card()
    with _patched(
        consume_return=(card, None),
        config={"provider": "mock"},
        adapter_raises=True,
    ) as h:
        body = await reveal_card(token="tok", tenant=h.tenant, db=h.db)

    assert body["pan"] is None
    assert body["cvv"] is None
    assert body["warning"]
    # Fail-closed: the token was consumed and committed before the provider was
    # ever called, so an outage cannot revive the link. Reviving it would be
    # indistinguishable from allowing a second reveal — and we cannot tell from
    # here whether the provider had already emitted the PAN.
    assert h.calls == ["consume", "audit", "commit", "provider"]
    h.db.rollback.assert_not_awaited()
