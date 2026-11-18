"""`POST /api/payments/corridor-quotes` — the multi-route optimizer's caller.

`services/corridor_quotes.compare_quotes` was fully built, documented and
tested but had **no production caller**: `grep` found no call site outside its
own module. That is a latent trap rather than a live defect — nothing
mis-routes today — but it means the first person to wire it up inherits every
untested assumption at once, on the money path.

This endpoint is the caller, deliberately scoped to what is safe to decide
without a treasury policy call: it PRICES a payable invoice across every
configured processor and returns the ranking. It is **advisory and read-only** —
no `Payment` is booked, no run is claimed, no invoice is touched, and the rail
that actually moves the money still comes from `payment_corridor.pick_corridor`.
Auto-routing money to the cheapest bidder is the product decision the followup
called it; showing a human the trade-off is not.

Also pinned here: `compare_quotes` caught only `UnknownPaymentProviderError`
from adapter construction, so a config entry that failed to construct for any
other reason took down the whole auction — including every other configured
rail — which is the exact property the unknown-name branch exists to prevent.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.services.payment_adapters import CorridorQuote, PaymentPayload

pytestmark = pytest.mark.asyncio

TENANT = "a"


def _user(uid):
    return SimpleNamespace(id=uid, full_name="Quote Reader", roles=["admin"])


def _org(org_id, providers):
    return SimpleNamespace(
        id=org_id,
        name="PyTest",
        slug="pytesta",
        settings={"payments": {"providers": providers}},
    )


async def _seed_invoice(mk, org_id) -> uuid.UUID:
    inv_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(Vendor(id=vendor_id, name="Acme Corp", organization_id=org_id))
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"CQ-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme Corp",
                vendor_id=vendor_id,
                amount=Decimal("10000.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return inv_id


def _adapter(name, *, available=True, flat=Decimal("0"), pct=Decimal("0"), eta=1):
    async def _quote(payload: PaymentPayload) -> CorridorQuote:
        return CorridorQuote(
            provider=name,
            method=payload.method,
            available=available,
            flat_fee=flat,
            pct_fee=pct,
            eta_business_days=eta,
        )

    return SimpleNamespace(provider_name=name, quote_payment=_quote)


async def test_cheapest_route_wins_and_savings_are_exact_decimal_strings(realdb):
    from app.api.payments import CorridorQuoteRequest, compare_corridor_quotes

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, info.org_id)

    cheap = _adapter("column", flat=Decimal("0.50"), eta=2)
    dear = _adapter("modern_treasury", flat=Decimal("25.00"), eta=1)

    async with mk() as db:
        with patch(
            "app.services.corridor_quotes.get_payment_adapter",
            side_effect=[cheap, dear],
        ):
            result = await compare_corridor_quotes(
                body=CorridorQuoteRequest(invoice_id=invoice_id, method="ach"),
                db=db,
                org=_org(info.org_id, [{"provider": "column"}, {"provider": "modern_treasury"}]),
                user=_user(info.users["admin"]),
                entity_id=None,
            )

    assert result["winner"]["provider"] == "column"
    assert result["winner"]["total_cost"] == "0.50"
    assert result["savings_vs_runner_up"] == "24.50"
    # Advisory by contract, in the payload — not only in the docstring.
    assert result["advisory"] is True
    # Money never crosses the boundary as a float.
    assert isinstance(result["amount"], str)


async def test_fastest_mode_prefers_the_lower_eta(realdb):
    from app.api.payments import CorridorQuoteRequest, compare_corridor_quotes

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, info.org_id)

    slow_cheap = _adapter("column", flat=Decimal("0.50"), eta=3)
    fast_dear = _adapter("modern_treasury", flat=Decimal("25.00"), eta=0)

    async with mk() as db:
        with patch(
            "app.services.corridor_quotes.get_payment_adapter",
            side_effect=[slow_cheap, fast_dear],
        ):
            result = await compare_corridor_quotes(
                body=CorridorQuoteRequest(invoice_id=invoice_id, mode="fastest"),
                db=db,
                org=_org(info.org_id, [{"provider": "column"}, {"provider": "modern_treasury"}]),
                user=_user(info.users["admin"]),
                entity_id=None,
            )

    assert result["winner"]["provider"] == "modern_treasury"
    assert result["mode"] == "fastest"


async def test_an_adapter_with_no_fee_schedule_is_listed_but_never_wins(realdb):
    """`PaymentAdapter.quote_payment` fails closed rather than fabricating a
    free/instant quote — an adapter that publishes no pricing must be skipped,
    not chosen on numbers nobody supplied."""
    from app.api.payments import CorridorQuoteRequest, compare_corridor_quotes

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, info.org_id)

    no_schedule = _adapter("modern_treasury", available=False)
    real = _adapter("column", flat=Decimal("0.50"))

    async with mk() as db:
        with patch(
            "app.services.corridor_quotes.get_payment_adapter",
            side_effect=[no_schedule, real],
        ):
            result = await compare_corridor_quotes(
                body=CorridorQuoteRequest(invoice_id=invoice_id),
                db=db,
                org=_org(info.org_id, [{"provider": "modern_treasury"}, {"provider": "column"}]),
                user=_user(info.users["admin"]),
                entity_id=None,
            )

    assert result["winner"]["provider"] == "column"
    assert any(
        q["provider"] == "modern_treasury" and not q["available"] for q in result["runners_up"]
    )
    # An unavailable route's cost is Decimal("Infinity") internally; that is
    # not a figure to render.
    unavailable = next(q for q in result["runners_up"] if not q["available"])
    assert unavailable["total_cost"] is None
    # No runner-up we could price against.
    assert result["savings_vs_runner_up"] == "0"


async def test_no_provider_can_quote_is_a_409_not_a_500(realdb):
    from app.api.payments import CorridorQuoteRequest, compare_corridor_quotes

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, info.org_id)

    async with mk() as db:
        with patch(
            "app.services.corridor_quotes.get_payment_adapter",
            side_effect=[_adapter("column", available=False)],
        ):
            with pytest.raises(HTTPException) as exc:
                await compare_corridor_quotes(
                    body=CorridorQuoteRequest(invoice_id=invoice_id),
                    db=db,
                    org=_org(info.org_id, [{"provider": "column"}]),
                    user=_user(info.users["admin"]),
                    entity_id=None,
                )
    assert exc.value.status_code == 409


async def test_an_out_of_scope_invoice_is_an_opaque_404(realdb):
    from app.api.payments import CorridorQuoteRequest, compare_corridor_quotes

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)

    async with mk() as db:
        with pytest.raises(HTTPException) as exc:
            await compare_corridor_quotes(
                body=CorridorQuoteRequest(invoice_id=uuid.uuid4()),
                db=db,
                org=_org(info.org_id, [{"provider": "column"}]),
                user=_user(info.users["admin"]),
                entity_id=None,
            )
    assert exc.value.status_code == 404


async def test_an_invoice_with_no_currency_is_refused_not_quoted_as_usd(realdb):
    """A blank currency is no currency: pricing it as USD would rank routes for
    a payment the run builder refuses (`currency_unknown`). No adapter is asked."""
    from sqlalchemy import update

    from app.api.payments import CorridorQuoteRequest, compare_corridor_quotes

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, info.org_id)
    async with mk() as s:
        await s.execute(update(Invoice).where(Invoice.id == invoice_id).values(currency=""))
        await s.commit()

    async with mk() as db:
        with patch("app.services.corridor_quotes.get_payment_adapter") as get_adapter:
            with pytest.raises(HTTPException) as exc:
                await compare_corridor_quotes(
                    body=CorridorQuoteRequest(invoice_id=invoice_id),
                    db=db,
                    org=_org(info.org_id, [{"provider": "column"}]),
                    user=_user(info.users["admin"]),
                    entity_id=None,
                )
    assert exc.value.status_code == 409
    assert exc.value.detail.startswith("currency_unknown:")
    get_adapter.assert_not_called()


# --------------------------------------------------------------------------- #
# The auction survives one unconstructable provider config
# --------------------------------------------------------------------------- #


async def test_one_unconstructable_provider_does_not_take_down_the_auction():
    """Only `UnknownPaymentProviderError` was caught, so an adapter `__init__`
    that raised for any other reason (a required credential missing from a
    half-filled config) killed the whole auction — including every other
    configured rail."""
    from app.services.corridor_quotes import compare_quotes

    payload = PaymentPayload(
        correlation_id=str(uuid.uuid4()),
        invoice_id=str(uuid.uuid4()),
        invoice_number="X-1",
        vendor_name="Acme",
        amount=Decimal("1000.00"),
        currency="USD",
        method="ach",
    )
    good = _adapter("column", flat=Decimal("0.50"))

    def _construct(cfg):
        if cfg.get("provider") == "broken":
            raise KeyError("originating_account_id")
        return good

    with patch("app.services.corridor_quotes.get_payment_adapter", side_effect=_construct):
        ranking = await compare_quotes(
            payload,
            {"payments": {"providers": [{"provider": "broken"}, {"provider": "column"}]}},
        )

    assert ranking.winner.provider == "column"
    broken = next(q for q in ranking.runners_up if q.provider == "broken")
    assert broken.available is False
    # The exception CLASS only — an SDK message can carry a partial account
    # number or key fragment, and `unavailable_reason` reaches a response body.
    assert broken.unavailable_reason == "provider_not_configured:KeyError"
    assert "originating_account_id" not in (broken.unavailable_reason or "")
