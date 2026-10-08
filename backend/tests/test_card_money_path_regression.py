"""End-to-end regression cover for the three virtual-card money-path defects
closed by PR #356 (tracker issue #321).

The pure decision functions are pinned in `test_card_rebate_base.py` and
`test_card_settlement_expiry.py`; the single-currency dashboard shape is pinned
against a mocked session in `test_card_dashboard.py`. This file drives the same
three fixes through the REAL code paths — the payment-run card leg, the signed
settlement webhook, and the dashboard's own SQL against a live Postgres — so a
regression that keeps the helpers intact while rewiring the call sites still
fails.

The three defects:

1. **The rebate base was the AUTHORIZED figure, and its fallback was the card's
   LIMIT.** `card.amount_charged or card.amount_limit`: `amount_charged` is
   stamped by the authorization event and was never updated at settlement, and
   the `or` reached for the authorization ceiling — a $10,000 card that settled
   $100 earned a rebate on $10,000.
2. **`card_settlement_block` ignored `expires_at`.** An aged-out card was a
   valid settlement target, so the payment went `completed` and the invoice
   `paid` while the vendor was never paid — worse than the double-spend the
   status check prevents, because there is no charge to reconcile against.
3. **`GET /api/cards/dashboard` mixed currencies.** Bare cross-currency `SUM`s
   over `amount_limit` / `amount_charged` / `CardRebate.amount` under no
   currency code at all.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.virtual_card import CardRebate, VirtualCard
from app.models.workflow import AuditLog

pytestmark = pytest.mark.asyncio

TENANT = "a"
_SECRET = "card-money-path-regression-secret"


# ---------------------------------------------------------------------------
# Fixtures / helpers (idiom copied from test_payment_card_duplicate_recovery
# and test_card_webhook_provider_cross_check)
# ---------------------------------------------------------------------------


def _user(uid: uuid.UUID | str = "user-1"):
    return SimpleNamespace(id=uid, full_name="Card Regression", roles=["admin"])


def _org(org_id: uuid.UUID, **settings_overrides):
    settings = {
        "payments": {"provider": "mock"},
        "cards": {
            "enabled": True,
            "program_type": "byok",
            "provider": "mock",
            "region": "US",
            "api_key": "test",
        },
    }
    settings.update(settings_overrides)
    return SimpleNamespace(id=org_id, name="PyTest", slug="pytesta", settings=settings)


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed_invoice(mk, org_id, *, amount: Decimal, currency: str = "USD"):
    """An approved, payable invoice with a real (screenable) vendor."""
    inv_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    corr = uuid.uuid4()
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(Vendor(id=vendor_id, name="Card Vendor", organization_id=org_id))
        s.add(
            Invoice(
                id=inv_id,
                entity_id=ent,
                invoice_number=f"CARDREG-{uuid.uuid4().hex[:8]}",
                vendor_name="Card Vendor",
                vendor_id=vendor_id,
                amount=amount,
                currency=currency,
                status=InvoiceStatus.approved,
                organization_id=org_id,
                correlation_id=corr,
            )
        )
        await s.commit()
    return SimpleNamespace(id=inv_id, correlation_id=corr, vendor_id=vendor_id, entity_id=ent)


async def _seed_run(mk, org_id, inv, *, amount: Decimal, creator_id):
    run_id = uuid.uuid4()
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                status="draft",
                total_amount=amount,
                initiated_by=creator_id,
                requires_cfo_approval=False,
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv.id,
                payment_run_id=run_id,
                amount=amount,
                method="virtual_card",
                status="pending",
                correlation_id=inv.correlation_id,
            )
        )
        await s.commit()
    return run_id, pay_id


def _card_row(
    inv,
    org_id,
    *,
    provider_card_id: str,
    limit: Decimal,
    status: str = "active",
    expires_at: datetime | None = None,
    currency: str = "USD",
    amount_charged: Decimal | None = None,
    provider: str = "mock",
) -> VirtualCard:
    return VirtualCard(
        invoice_id=inv.id,
        organization_id=org_id,
        entity_id=inv.entity_id,
        vendor_id=inv.vendor_id,
        correlation_id=inv.correlation_id,
        card_provider=provider,
        provider_card_id=provider_card_id,
        last_four="9999",
        amount_limit=limit,
        amount_charged=amount_charged,
        currency=currency,
        status=status,
        expires_at=expires_at,
    )


@contextlib.contextmanager
def _ambient_patches(*extra):
    """Silence the surrounding machinery (ERP sync, sanctions) so each test is
    about the card leg only. `transition_invoice` is deliberately NOT patched:
    whether the invoice advances is the assertion."""
    with contextlib.ExitStack() as stack:
        for ctx in (
            patch("app.services.payment_erp_sync.dispatch_payment_sync", new_callable=AsyncMock),
            patch(
                "app.services.compliance.check_payment_compliance",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(verdict="allow", reasons=[]),
            ),
            *extra,
        ):
            stack.enter_context(ctx)
        yield


async def _audit_actions(mk, entity_id) -> list[str]:
    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.entity_id == entity_id)))
            .scalars()
            .all()
        )
    return [r.action for r in rows]


# ===========================================================================
# 1. An expired card cannot settle a payment
#
# Pre-fix, every assertion in this section reported the opposite: the payment
# was `completed`, the invoice advanced, and the run rolled up as a success —
# against a card the network would refuse.
# ===========================================================================


async def _run_against_a_pre_existing_card(realdb, *, expires_at, limit=None, status="active"):
    """Mint a card into the invoice's live slot, then execute a `virtual_card`
    run over that invoice so the leg CONVERGES rather than mints."""
    from app.api.payments import execute_payment_run

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("100.00")

    inv = await _seed_invoice(mk, org_id, amount=amount)
    async with mk() as s:
        card = _card_row(
            inv,
            org_id,
            provider_card_id=f"card_{uuid.uuid4().hex[:10]}",
            limit=limit or amount,
            status=status,
            expires_at=expires_at,
        )
        s.add(card)
        await s.commit()
        card_id = card.id

    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )

    from app.services.card_adapters.mock_adapter import MockCardAdapter

    real_create = MockCardAdapter.create_card
    minted = 0

    async def _spy(self, payload):
        nonlocal minted
        minted += 1
        return await real_create(self, payload)

    with _ambient_patches(patch.object(MockCardAdapter, "create_card", _spy)):
        async with realdb.sessionmaker(TENANT)() as db:
            res = await execute_payment_run(
                run_id=run_id,
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                entity_id=None,
            )
            await db.commit()

    async with mk() as s:
        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        run = (await s.execute(select(PaymentRun).where(PaymentRun.id == run_id))).scalar_one()
        card = (await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))).scalar_one()
        invoice = (await s.execute(select(Invoice).where(Invoice.id == inv.id))).scalar_one()
        rebates = (
            (await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id)))
            .scalars()
            .all()
        )
    return SimpleNamespace(
        result=res,
        payment=pay,
        run=run,
        card=card,
        invoice=invoice,
        rebates=rebates,
        minted=minted,
        mk=mk,
        payment_id=pay_id,
        card_id=card_id,
    )


async def test_an_expired_card_fails_the_payment_and_leaves_the_invoice_unpaid(realdb):
    """The whole defect in one test. The vendor holds a dead card; nothing can
    move against it, so the payment must NOT assert that money moved and the
    invoice must NOT advance towards `paid`."""
    out = await _run_against_a_pre_existing_card(
        realdb, expires_at=datetime.now(UTC) - timedelta(days=30)
    )

    assert out.payment.status == "failed"
    assert out.payment.failure_reason == "card_expired"
    assert out.payment.completed_at is not None
    # The money assertions that used to be made falsely.
    assert out.payment.status != "completed"
    assert out.payment.reference is None
    assert out.invoice.status == InvoiceStatus.approved
    assert out.invoice.status not in (InvoiceStatus.payment_scheduled, InvoiceStatus.paid)
    # The card is untouched — we did not link it, spend it, or rebate on it.
    assert out.card.status == "active"
    assert out.card.payment_id is None
    assert out.card.amount_charged is None
    assert out.rebates == []
    # And no second provider card was minted behind the refusal.
    assert out.minted == 0
    assert out.result["status"] == "failed"


async def test_the_expired_refusal_is_recorded_in_the_append_only_trail(realdb):
    """Every payment status transition writes a log row. The refusal must show
    as `payment.failed` — and never as the `card.reused` / `payment.completed`
    pair a converge would have written."""
    out = await _run_against_a_pre_existing_card(
        realdb, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    payment_actions = await _audit_actions(out.mk, out.payment_id)
    card_actions = await _audit_actions(out.mk, out.card_id)

    assert "payment.failed" in payment_actions
    assert "payment.completed" not in payment_actions
    assert "card.reused" not in card_actions
    assert "card.generated" not in card_actions


async def test_a_live_card_still_settles_the_payment(realdb):
    """The control. Same fixture, an expiry in the future — the documented
    converge path must be unchanged, so the refusal above is a real refusal and
    not a broken leg."""
    out = await _run_against_a_pre_existing_card(
        realdb, expires_at=datetime.now(UTC) + timedelta(days=90)
    )

    assert out.payment.status == "completed", out.payment.failure_reason
    assert out.payment.reference and "9999" in out.payment.reference
    assert out.invoice.status == InvoiceStatus.payment_scheduled
    assert out.card.payment_id == out.payment_id
    assert "card.reused" in await _audit_actions(out.mk, out.card_id)


async def test_a_card_with_no_expiry_still_settles_the_payment(realdb):
    """`expires_at` is nullable and several providers omit it. Refusing on a
    missing value would have blocked every such card — a fix worse than the
    defect."""
    out = await _run_against_a_pre_existing_card(realdb, expires_at=None)

    assert out.payment.status == "completed", out.payment.failure_reason
    assert out.invoice.status == InvoiceStatus.payment_scheduled


async def test_a_spent_and_expired_card_reports_the_spend(realdb):
    """Precedence, through the real leg: the money that already moved is the
    more actionable fact for the operator reading `failure_reason`."""
    out = await _run_against_a_pre_existing_card(
        realdb,
        expires_at=datetime.now(UTC) - timedelta(days=1),
        status="charged",
    )

    assert out.payment.status == "failed"
    assert out.payment.failure_reason == "card_already_charged"
    assert out.invoice.status == InvoiceStatus.approved


async def test_an_expired_card_too_small_for_the_payable_reports_the_expiry(realdb):
    """Raising the limit would not make a dead card settleable, so the expiry
    is the reason an operator needs."""
    out = await _run_against_a_pre_existing_card(
        realdb,
        expires_at=datetime.now(UTC) - timedelta(days=1),
        limit=Decimal("1.00"),
    )

    assert out.payment.failure_reason == "card_expired"
    assert out.invoice.status == InvoiceStatus.approved


async def test_the_expired_refusal_is_reached_before_any_money_state_changes(realdb):
    """A failed card leg must leave the run rolled up (not stranded
    `executing`) and must not have consumed the invoice's payability — a fresh,
    correctly-carded run has to still be possible."""
    out = await _run_against_a_pre_existing_card(
        realdb, expires_at=datetime.now(UTC) - timedelta(days=5)
    )

    assert out.run.status == "failed"
    assert out.run.executed_at is not None
    # The invoice is still in a payable state, so AP can cancel the dead card
    # and pay it properly — the failure is recoverable, not a dead end.
    from app.api.payments import PAYABLE_INVOICE_STATUSES

    assert out.invoice.status.value in PAYABLE_INVOICE_STATUSES


# ===========================================================================
# 2. The rebate is earned on the SETTLED amount — never the limit
#
# Driven through the real signed settlement webhook, so the figure the
# processor reported and the figure we rebate on are compared end to end.
# ===========================================================================


async def _set_card_settings(realdb, org_id, **extra) -> None:
    from app.services.provider_credentials import update_secrets

    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings["cards"] = {**(settings.get("cards") or {}), **extra}
        org.settings = settings
        # The signing secret is sealed, not settings (services/provider_credentials).
        await update_secrets(s, org_id, "cards", {"webhook_signing_secret": _SECRET}, [])
        await s.commit()


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _settlement_body(token: str, *, amount, event_id: str | None = None) -> bytes:
    payload = {
        "eventType": "transaction.settled",
        "webhookId": event_id or str(uuid.uuid4()),
        "cardHashId": token,
        "merchantName": "Acme Fuel",
    }
    if amount is not None:
        payload["amount"] = amount
    return json.dumps(payload).encode("utf-8")


async def _seed_charged_card(
    mk, org_id, *, token: str, limit: Decimal, amount_charged: Decimal | None
) -> uuid.UUID:
    """A card in the state a settlement event finds it: authorized (`charged`)
    with the AUTH figure on `amount_charged`, and a much larger limit.

    Issued by `nium` because that is the webhook path these tests post to —
    the handler cross-checks the URL's `{provider}` segment against
    `VirtualCard.card_provider`, and Nium reports amounts in MAJOR units.
    """
    inv = await _seed_invoice(mk, org_id, amount=limit)
    async with mk() as s:
        card = _card_row(
            inv,
            org_id,
            provider_card_id=token,
            limit=limit,
            status="charged",
            amount_charged=amount_charged,
            provider="nium",
        )
        s.add(card)
        await s.commit()
        return card.id


async def _post_settlement(realdb, body: bytes) -> int:
    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            "/api/cards/webhook/nium",
            content=body,
            headers={"Content-Type": "application/json", "Webhook-Signature": _sign(body)},
        )
    return resp.status_code


async def _read_settlement(mk, card_id):
    async with mk() as s:
        card = (await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))).scalar_one()
        rebates = (
            (await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id)))
            .scalars()
            .all()
        )
        audits = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == card_id, AuditLog.action == "card.settled"
                    )
                )
            )
            .scalars()
            .all()
        )
    return card, rebates, audits


async def test_rebate_is_computed_on_the_settled_amount_not_the_authorization(realdb):
    """A partial capture: authorized $100, settled $80, on a $10,000 card. The
    rebate is 1% of 80 = 0.80. Pre-fix it was 1% of the AUTHORIZED 100 = 1.00,
    because `amount_charged` is stamped by the auth event and never updated."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("10000.00"), amount_charged=Decimal("100.00")
    )
    await _set_card_settings(realdb, org_id)

    assert await _post_settlement(realdb, _settlement_body(token, amount="80.00")) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert card.status == "completed"
    # The settled figure is persisted — the card detail, the spend rollups and
    # the corporate-card feed all read `amount_charged`.
    assert card.amount_charged == Decimal("80.00")
    assert len(rebates) == 1
    assert rebates[0].amount == Decimal("0.80")
    assert rebates[0].amount != Decimal("1.00"), "rebated on the authorization"
    assert len(audits) == 1
    assert audits[0].details["rebate_base"] == "80.00"
    assert audits[0].details["rebate_base_source"] == "settled"


async def test_a_settlement_with_no_amount_rebates_on_the_charged_figure(realdb):
    """Some rails send a bare settlement envelope. The AUTHORIZED figure is
    then the best evidence available — and the audit row says so, so a
    reconciliation against the processor's statement can tell the two apart."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("10000.00"), amount_charged=Decimal("100.00")
    )
    await _set_card_settings(realdb, org_id)

    assert await _post_settlement(realdb, _settlement_body(token, amount=None)) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    # 1% of the $100 authorization — NOT 1% of the $10,000 limit.
    assert rebates[0].amount == Decimal("1.00")
    assert card.amount_charged == Decimal("100.00")
    assert audits[0].details["rebate_base"] == "100.00"
    assert audits[0].details["rebate_base_source"] == "authorized"


async def test_no_usable_amount_anywhere_rebates_on_zero_not_the_limit(realdb):
    """The headline case from the lead: a $10,000 card, a settlement carrying
    no amount, and no authorization figure either. Pre-fix the `or` fallback
    reached the card's authorization CEILING and booked a $100.00 rebate on
    money that was never even claimed to have moved."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("10000.00"), amount_charged=None
    )
    await _set_card_settings(realdb, org_id)

    assert await _post_settlement(realdb, _settlement_body(token, amount=0)) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert card.status == "completed"
    assert rebates[0].amount == Decimal("0.00")
    assert rebates[0].amount != Decimal("100.00"), "rebated on the card's limit"
    assert audits[0].details["rebate_base"] == "0"
    assert audits[0].details["rebate_base_source"] == "unknown"
    # The limit is never written onto the spend column either.
    assert card.amount_charged is None


async def test_an_over_settlement_rebates_on_what_actually_settled(realdb):
    """A tip / fuel adjustment settles ABOVE the auth. The rule is "what
    moved", so the rebate follows it up — the old expression could only ever
    report the auth, under-rebating us."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("200.00"), amount_charged=Decimal("100.00")
    )
    await _set_card_settings(realdb, org_id)

    assert await _post_settlement(realdb, _settlement_body(token, amount="115.00")) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert card.amount_charged == Decimal("115.00")
    assert rebates[0].amount == Decimal("1.15")
    assert audits[0].details["rebate_base_source"] == "settled"


async def test_a_settlement_above_the_limit_is_still_the_settled_base(realdb):
    """Over-authorization is a processor/network anomaly for the settlement
    verifier to judge — but it must not silently re-become a rebate on the
    limit, which is what the old fallback would look like at exactly this
    value."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("100.00"), amount_charged=Decimal("100.00")
    )
    await _set_card_settings(realdb, org_id)

    assert await _post_settlement(realdb, _settlement_body(token, amount="150.00")) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert card.amount_charged == Decimal("150.00")
    assert rebates[0].amount == Decimal("1.50")
    assert audits[0].details["rebate_base"] == "150.00"


async def test_the_rebate_is_exact_decimal_at_the_negotiated_rate(realdb):
    """Money is exact end to end: the org's negotiated 1.25% on a settled
    33.33 is 0.416625, quantized ROUND_HALF_UP to 0.42 and stored in
    `Numeric(15, 2)` — no float hop anywhere (0.42 would come back 0.4199…)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("500.00"), amount_charged=Decimal("500.00")
    )
    await _set_card_settings(realdb, org_id, rebate_rate="0.0125")

    assert await _post_settlement(realdb, _settlement_body(token, amount="33.33")) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert isinstance(rebates[0].amount, Decimal)
    assert rebates[0].amount == Decimal("0.42")
    assert str(rebates[0].amount) == "0.42"
    assert rebates[0].rate == Decimal("0.0125")
    assert card.amount_charged == Decimal("33.33")
    # The base rides the audit row as an exact string, never a float repr.
    assert audits[0].details["rebate_base"] == "33.33"
    assert audits[0].details["rebate_amount"] == "0.42"


async def test_a_replayed_settlement_event_does_not_re_rebate_or_re_stamp(realdb):
    """Idempotency, on the write that mints money owed to us. The provider
    retries on any non-2xx, so the same event id arriving twice must be
    deduped: one rebate, one audit row, and the settled figure not rewritten."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("10000.00"), amount_charged=Decimal("100.00")
    )
    await _set_card_settings(realdb, org_id)

    event_id = str(uuid.uuid4())
    body = _settlement_body(token, amount="80.00", event_id=event_id)
    assert await _post_settlement(realdb, body) == 204
    assert await _post_settlement(realdb, body) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert len(rebates) == 1
    assert len(audits) == 1
    assert card.amount_charged == Decimal("80.00")


async def test_a_second_distinct_settlement_cannot_overwrite_the_settled_figure(realdb):
    """A DIFFERENT event id past the dedup window still finds the card
    `completed`, so the settlement branch is skipped entirely. Persisting the
    settled amount must not open a path for a later event to restate spend or
    mint a second rebate."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("10000.00"), amount_charged=Decimal("100.00")
    )
    await _set_card_settings(realdb, org_id)

    assert await _post_settlement(realdb, _settlement_body(token, amount="80.00")) == 204
    assert await _post_settlement(realdb, _settlement_body(token, amount="9000.00")) == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert card.amount_charged == Decimal("80.00")
    assert len(rebates) == 1
    assert rebates[0].amount == Decimal("0.80")
    assert len(audits) == 1


async def test_the_settlement_audit_row_is_pii_free(realdb):
    """The trail carries the last four (documented) and exact string Decimals —
    never a PAN, and the new `rebate_base` fields must not change that."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    card_id = await _seed_charged_card(
        mk, org_id, token=token, limit=Decimal("500.00"), amount_charged=Decimal("400.00")
    )
    await _set_card_settings(realdb, org_id)

    assert await _post_settlement(realdb, _settlement_body(token, amount="400.00")) == 204

    _card, _rebates, audits = await _read_settlement(mk, card_id)
    details = audits[0].details
    assert set(details) == {
        "last_four",
        "from",
        "to",
        "rebate_amount",
        "rebate_rate",
        "rebate_created",
        "rebate_base",
        "rebate_base_source",
    }
    assert details["last_four"] == "9999"
    for key in ("rebate_amount", "rebate_rate", "rebate_base"):
        assert isinstance(details[key], str), key
    assert details["rebate_created"] is True
    # No provider token / PAN anywhere in the row.
    assert token not in json.dumps(details)


async def test_an_authorization_event_still_stamps_the_authorized_figure(realdb):
    """The auth leg is unchanged — it is the settlement branch that reads the
    event's own amount now. A card that has only been authorized still carries
    the auth figure (and no rebate)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    inv = await _seed_invoice(mk, org_id, amount=Decimal("100.00"))
    async with mk() as s:
        card = _card_row(
            inv, org_id, provider_card_id=token, limit=Decimal("10000.00"), provider="nium"
        )
        s.add(card)
        await s.commit()
        card_id = card.id
    await _set_card_settings(realdb, org_id)

    body = json.dumps(
        {
            "eventType": "authorization.approved",
            "webhookId": str(uuid.uuid4()),
            "cardHashId": token,
            "amount": "100.00",
            "merchantName": "Acme Fuel",
        }
    ).encode("utf-8")
    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            "/api/cards/webhook/nium",
            content=body,
            headers={"Content-Type": "application/json", "Webhook-Signature": _sign(body)},
        )
    assert resp.status_code == 204

    card, rebates, audits = await _read_settlement(mk, card_id)
    assert card.status == "charged"
    assert card.amount_charged == Decimal("100.00")
    assert rebates == []
    assert audits == []


# ===========================================================================
# 3. Every dashboard figure is a quantity in ONE currency
#
# Driven against real SQL (the `CardRebate` -> `VirtualCard` join, the
# `coalesce`/`upper` on the card's currency, the entity scope) rather than a
# mocked session, because the join IS the fix: `CardRebate` has no currency
# column, so a rebate's currency is only knowable through its card.
# ===========================================================================


async def _add_card(
    mk,
    org_id,
    *,
    currency: str | None,
    limit: str,
    status: str = "active",
    charged: str | None = None,
    charged_at: datetime | None = None,
    rebate: tuple[str, str] | None = None,
    period: str | None = None,
    entity_id=None,
) -> uuid.UUID:
    """One card (+ optionally its rebate) denominated in `currency`.

    Written directly rather than through the API so the currency can be set
    per-row — the point is a card programme running more than one.
    """
    inv = await _seed_invoice(mk, org_id, amount=Decimal(limit), currency=currency or "USD")
    async with mk() as s:
        card = _card_row(
            inv,
            org_id,
            provider_card_id=f"card_{uuid.uuid4().hex[:10]}",
            limit=Decimal(limit),
            status=status,
            amount_charged=Decimal(charged) if charged is not None else None,
            currency=currency or "USD",
        )
        if currency is None:
            card.currency = None
        if entity_id is not None:
            card.entity_id = entity_id
        card.charged_at = charged_at
        s.add(card)
        await s.flush()
        if rebate is not None:
            amount, rebate_status = rebate
            s.add(
                CardRebate(
                    virtual_card_id=card.id,
                    amount=Decimal(amount),
                    rate=Decimal("0.0100"),
                    status=rebate_status,
                    period=period or datetime.now(UTC).strftime("%Y-%m"),
                    organization_id=org_id,
                )
            )
        await s.commit()
        return card.id


async def _dashboard(realdb, org_id, *, settings=None, entity_id=None):
    """Call the endpoint function against a REAL tenant session so the money
    fields come back as `Decimal` (the wire format is a JSON number)."""
    from app.api.cards import card_dashboard

    org = SimpleNamespace(id=org_id, settings=settings if settings is not None else {})
    async with realdb.sessionmaker(TENANT)() as s:
        return await card_dashboard(db=s, org=org, user=_user(), entity_id=entity_id)


async def test_active_card_value_counts_only_the_reporting_currency(realdb):
    """USD 1,000 + EUR 500 + GBP 250 of live limits reports 1,000 under USD —
    not 1,750, which is a quantity in no currency at all."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="USD", limit="1000.00")
    await _add_card(mk, org_id, currency="EUR", limit="500.00")
    await _add_card(mk, org_id, currency="GBP", limit="250.00")

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.currency == "USD"
    assert res.active_cards == 1
    assert res.active_cards_value == Decimal("1000.00")
    assert res.active_cards_value != Decimal("1750.00")
    assert res.excluded_card_count == 2


async def test_rebate_totals_are_attributed_through_the_card_they_were_earned_on(realdb):
    """`CardRebate` has no currency column. The rollups join `VirtualCard`, so
    a rebate earned on a EUR card is a EUR rebate and cannot land in the USD
    total — the bare SUM had no way to know."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="USD", limit="1000.00", rebate=("10.00", "confirmed"))
    await _add_card(mk, org_id, currency="EUR", limit="1000.00", rebate=("900.00", "confirmed"))
    await _add_card(mk, org_id, currency="GBP", limit="1000.00", rebate=("70.00", "paid_out"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.rebates_ytd == Decimal("10.00")
    assert res.rebates_this_month == Decimal("10.00")
    assert res.rebates_ytd != Decimal("980.00")
    assert res.rebates_ytd_by_status.confirmed_total == Decimal("10.00")
    assert res.rebates_ytd_by_status.paid_out_total == Decimal("0")
    assert res.excluded_rebate_count == 2


async def test_the_status_breakdown_is_also_single_currency(realdb):
    """Every money field in the response, not just the headline — the split
    the frontend rebate panel renders beside it must agree with it."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="USD", limit="100.00", rebate=("1.00", "pending"))
    await _add_card(mk, org_id, currency="USD", limit="100.00", rebate=("2.00", "confirmed"))
    await _add_card(mk, org_id, currency="USD", limit="100.00", rebate=("3.00", "paid_out"))
    await _add_card(mk, org_id, currency="JPY", limit="100.00", rebate=("500.00", "pending"))
    await _add_card(mk, org_id, currency="JPY", limit="100.00", rebate=("600.00", "confirmed"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    month = res.rebates_this_month_by_status
    assert (month.pending_total, month.confirmed_total, month.paid_out_total) == (
        Decimal("1.00"),
        Decimal("2.00"),
        Decimal("3.00"),
    )
    ytd = res.rebates_ytd_by_status
    assert (ytd.pending_total, ytd.confirmed_total, ytd.paid_out_total) == (
        Decimal("1.00"),
        Decimal("2.00"),
        Decimal("3.00"),
    )
    # Realized only: 2 + 3, never the 1.00 pending and never the JPY rows.
    assert res.rebates_this_month == Decimal("5.00")
    assert res.excluded_rebate_count == 2


async def test_spend_this_month_counts_only_the_reporting_currency(realdb):
    """`amount_charged` was summed across currencies too — the "spend" headline
    beside the rebate figures."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    now = datetime.now(UTC)
    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="1000.00",
        status="completed",
        charged="400.00",
        charged_at=now,
    )
    await _add_card(
        mk,
        org_id,
        currency="EUR",
        limit="1000.00",
        status="completed",
        charged="900.00",
        charged_at=now,
    )

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.spend_this_month == Decimal("400.00")
    assert res.spend_this_month != Decimal("1300.00")


async def test_the_currency_follows_the_org_and_so_do_the_figures(realdb):
    """With the org on EUR the EUR rows are the ones that count — proving the
    filter reads the canonical resolver rather than a hardcoded USD."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="USD", limit="1000.00", rebate=("10.00", "confirmed"))
    await _add_card(mk, org_id, currency="EUR", limit="500.00", rebate=("5.00", "confirmed"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "EUR"})

    assert res.currency == "EUR"
    assert res.active_cards_value == Decimal("500.00")
    assert res.rebates_ytd == Decimal("5.00")
    assert res.excluded_card_count == 1
    assert res.excluded_rebate_count == 1


async def test_the_currency_resolution_chain_is_the_shared_one(realdb):
    """Not `settings.reporting_currency` alone: an org that set only a home
    currency must be labelled — and filtered — by it."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="GBP", limit="800.00")
    await _add_card(mk, org_id, currency="USD", limit="100.00")

    res = await _dashboard(realdb, org_id, settings={"payments": {"home_currency": "GBP"}})

    assert res.currency == "GBP"
    assert res.active_cards_value == Decimal("800.00")
    assert res.excluded_card_count == 1


async def test_a_single_currency_programme_reports_no_exclusions(realdb):
    """The common case must be untouched: the same figures the old bare SUM
    produced, and nothing disclosed as left out."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="USD", limit="1000.00", rebate=("10.00", "confirmed"))
    await _add_card(mk, org_id, currency="USD", limit="2500.00", rebate=("25.00", "paid_out"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.active_cards == 2
    assert res.active_cards_value == Decimal("3500.00")
    assert res.rebates_ytd == Decimal("35.00")
    assert res.excluded_card_count == 0
    assert res.excluded_rebate_count == 0


async def test_an_empty_programme_reports_zeroes_not_nulls(realdb):
    """No cards and no rebates: every figure is an exact zero Decimal and the
    currency is still declared, so the panel has something to render."""
    org_id = realdb.info(TENANT).org_id

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.currency == "USD"
    assert res.active_cards == 0
    for value in (
        res.active_cards_value,
        res.spend_this_month,
        res.rebates_ytd,
        res.rebates_this_month,
        res.projected_annual_rebates,
    ):
        assert value == Decimal("0")
    assert res.excluded_card_count == 0
    assert res.excluded_rebate_count == 0


async def test_cards_without_rebates_and_rebates_without_in_currency_cards(realdb):
    """Two half-populated shapes that used to divide by an assumption: live
    cards that have earned nothing yet, and rebates that ALL belong to
    foreign-currency cards (so the in-currency total is a true zero, with the
    remainder disclosed rather than swallowed)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="USD", limit="1000.00")  # no rebate yet
    await _add_card(mk, org_id, currency="EUR", limit="100.00", rebate=("50.00", "confirmed"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.active_cards == 1
    assert res.active_cards_value == Decimal("1000.00")
    assert res.rebates_ytd == Decimal("0")
    assert res.projected_annual_rebates == Decimal("0")
    assert res.excluded_card_count == 1
    assert res.excluded_rebate_count == 1


async def test_a_lowercased_card_currency_is_the_same_currency(realdb):
    """`VirtualCard.currency` is a free-form `String(3)`. A row written `usd`
    must not be excluded as if it were foreign — the filter upper-cases both
    sides."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency="usd", limit="700.00", rebate=("7.00", "confirmed"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.active_cards_value == Decimal("700.00")
    assert res.rebates_ytd == Decimal("7.00")
    assert res.excluded_card_count == 0
    assert res.excluded_rebate_count == 0


async def test_a_card_with_no_currency_is_treated_as_the_reporting_currency(realdb):
    """The column is nullable. A NULL is an unstamped row, not a foreign one —
    coalescing it in keeps a legacy card visible instead of silently deleting
    it from every figure (the same trade `vendor_matching` makes for a NULL
    entity)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    await _add_card(mk, org_id, currency=None, limit="300.00", rebate=("3.00", "confirmed"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.active_cards == 1
    assert res.active_cards_value == Decimal("300.00")
    assert res.rebates_ytd == Decimal("3.00")
    assert res.excluded_card_count == 0
    assert res.excluded_rebate_count == 0


async def test_a_prior_year_rebate_is_outside_year_to_date(realdb):
    """The YTD window is bounded at both ends, and the exclusion count shares
    it — a foreign-currency rebate from last year must not be reported as this
    year's remainder."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    last_year = f"{datetime.now(UTC).year - 1}-06"
    await _add_card(
        mk, org_id, currency="EUR", limit="100.00", rebate=("50.00", "confirmed"), period=last_year
    )
    await _add_card(mk, org_id, currency="USD", limit="100.00", rebate=("1.00", "confirmed"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    assert res.rebates_ytd == Decimal("1.00")
    assert res.excluded_rebate_count == 0


async def test_every_money_field_is_an_exact_decimal(realdb):
    """The money invariant at the response boundary: `Decimal` in Python (the
    JSON hop is the only float), and the totals are exact to the cent rather
    than a binary-float sum of many small rebates."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    for _ in range(3):
        await _add_card(mk, org_id, currency="USD", limit="10.10", rebate=("0.10", "confirmed"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    for value in (
        res.active_cards_value,
        res.spend_this_month,
        res.rebates_this_month,
        res.rebates_ytd,
        res.projected_annual_rebates,
        res.rebates_ytd_by_status.confirmed_total,
    ):
        assert isinstance(value, Decimal), value
        assert not isinstance(value, float)
    assert res.active_cards_value == Decimal("30.30")
    assert res.rebates_ytd == Decimal("0.30")


async def test_the_entity_scope_and_the_currency_filter_compose(realdb):
    """Both narrowings apply — a EUR card in the selected entity is still
    excluded by currency, and a USD card in ANOTHER entity is still excluded by
    entity (and is not counted as a currency exclusion either)."""
    from app.models.entity import Entity

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        default_entity = await _default_entity_id(s)
        other = Entity(
            organization_id=org_id,
            name="Subsidiary",
            slug=f"sub-{uuid.uuid4().hex[:6]}",
            currency="USD",
            is_default=False,
        )
        s.add(other)
        await s.commit()
        other_entity = other.id

    await _add_card(mk, org_id, currency="USD", limit="100.00", entity_id=default_entity)
    await _add_card(mk, org_id, currency="EUR", limit="200.00", entity_id=default_entity)
    await _add_card(mk, org_id, currency="USD", limit="400.00", entity_id=other_entity)

    res = await _dashboard(
        realdb, org_id, settings={"reporting_currency": "USD"}, entity_id=default_entity
    )

    assert res.active_cards == 1
    assert res.active_cards_value == Decimal("100.00")
    assert res.excluded_card_count == 1  # the EUR card in this entity, only


async def test_the_endpoint_declares_its_currency_over_http_too(realdb):
    """The wire shape the frontend reads: the new `currency` /
    `excluded_*_count` fields are present on the real response, so a partial
    figure can be rendered as visibly partial."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings["reporting_currency"] = "USD"
        org.settings = settings
        await s.commit()
    await _add_card(mk, org_id, currency="USD", limit="1000.00", rebate=("10.00", "confirmed"))
    await _add_card(mk, org_id, currency="EUR", limit="9999.00", rebate=("99.00", "confirmed"))

    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get("/api/cards/dashboard")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == "USD"
    assert Decimal(str(body["active_cards_value"])) == Decimal("1000.00")
    assert Decimal(str(body["rebates_ytd"])) == Decimal("10.00")
    assert body["excluded_card_count"] == 1
    assert body["excluded_rebate_count"] == 1


async def test_the_dashboard_never_double_counts_a_rebate_across_the_join(realdb):
    """The rebate rollups gained a join. A join that fanned out (or lost rows)
    would restate the money — one rebate per card, counted once."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    for _ in range(4):
        await _add_card(mk, org_id, currency="USD", limit="100.00", rebate=("2.50", "paid_out"))

    res = await _dashboard(realdb, org_id, settings={"reporting_currency": "USD"})

    async with mk() as s:
        n_rebates = (await s.execute(select(func.count()).select_from(CardRebate))).scalar_one()
    assert n_rebates == 4
    assert res.rebates_ytd == Decimal("10.00")
    assert res.rebates_ytd_by_status.paid_out_total == Decimal("10.00")


# --------------------------------------------------------------------------- #
# The rebate rollups describe the SELECTED entity, not the whole org
# --------------------------------------------------------------------------- #


async def _other_entity(mk, org_id):
    """A second (non-default) entity in the same tenant."""
    from app.models.entity import Entity

    async with mk() as s:
        ent = Entity(
            organization_id=org_id, name="Subsidiary B", slug=f"sub-{uuid.uuid4().hex[:6]}"
        )
        s.add(ent)
        await s.commit()
        return ent.id


async def _default_entity(mk):
    async with mk() as s:
        return await _default_entity_id(s)


async def test_the_rebate_rollups_are_scoped_to_the_selected_entity(realdb):
    """A subsidiary's rebate must not appear in the parent entity's figures.

    `CardRebate` carries no `entity_id`, so its entity is only knowable through
    its card — the same indirection the currency fix needed. `GET /rebates`
    already scoped its LIST that way; the dashboard's rollups did not, so a
    multi-entity tenant read entity-scoped card figures beside org-wide rebate
    figures and the headline could not be reconciled against the list an
    operator drills into. Pre-fix this reported 7.00 under the default entity.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    other = await _other_entity(mk, org_id)
    period = datetime.now(UTC).strftime("%Y-%m")

    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="500.00",
        rebate=("7.00", "confirmed"),
        period=period,
        entity_id=other,
    )
    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="400.00",
        rebate=("3.00", "confirmed"),
        period=period,
    )

    res = await _dashboard(realdb, org_id, entity_id=await _default_entity(mk))
    assert res.rebates_ytd == Decimal("3.00")
    assert res.rebates_this_month == Decimal("3.00")


async def test_the_rebate_status_breakdown_is_scoped_too(realdb):
    """The pending/confirmed/paid_out split must not widen where the total narrowed.

    A breakdown that disagrees with the total beside it is the same defect in a
    subtler place — the reader reconciles the three against the headline.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    other = await _other_entity(mk, org_id)
    period = datetime.now(UTC).strftime("%Y-%m")

    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="500.00",
        rebate=("9.00", "pending"),
        period=period,
        entity_id=other,
    )
    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="500.00",
        rebate=("8.00", "paid_out"),
        period=period,
        entity_id=other,
    )
    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="400.00",
        rebate=("2.00", "pending"),
        period=period,
    )

    res = await _dashboard(realdb, org_id, entity_id=await _default_entity(mk))
    assert res.rebates_ytd_by_status.pending_total == Decimal("2.00")
    assert res.rebates_ytd_by_status.paid_out_total == Decimal("0.00")
    assert res.rebates_this_month_by_status.pending_total == Decimal("2.00")
    # And the realized total still equals confirmed + paid_out of the same set.
    assert res.rebates_ytd == (
        res.rebates_ytd_by_status.confirmed_total + res.rebates_ytd_by_status.paid_out_total
    )


async def test_the_rebate_exclusion_count_describes_the_same_entity_it_discloses(realdb):
    """`excluded_rebate_count` explains a figure — so it must share its scope.

    An org-wide count against an entity-scoped figure over-discloses: the page
    says "N rebates left out" naming rows that were never in scope to begin
    with, which is as misleading as under-disclosing.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    other = await _other_entity(mk, org_id)
    period = datetime.now(UTC).strftime("%Y-%m")

    # Foreign-currency rebate in ANOTHER entity: out of scope twice over, and
    # must not be counted as this entity's currency exclusion.
    await _add_card(
        mk,
        org_id,
        currency="EUR",
        limit="500.00",
        rebate=("5.00", "confirmed"),
        period=period,
        entity_id=other,
    )
    # Foreign-currency rebate in THIS entity: the one genuine exclusion.
    await _add_card(
        mk,
        org_id,
        currency="GBP",
        limit="500.00",
        rebate=("4.00", "confirmed"),
        period=period,
    )

    res = await _dashboard(realdb, org_id, entity_id=await _default_entity(mk))
    assert res.excluded_rebate_count == 1
    assert res.rebates_ytd == Decimal("0.00")


async def test_the_consolidated_view_still_sees_every_entity(realdb):
    """No selected entity = the whole tenant, so the fix must not narrow that.

    `apply_entity_scope` is a no-op on `entity_id=None`, which is the header-less
    consolidated read; a scope that always narrowed would silently delete the
    subsidiaries from the group view.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    other = await _other_entity(mk, org_id)
    period = datetime.now(UTC).strftime("%Y-%m")

    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="500.00",
        rebate=("7.00", "confirmed"),
        period=period,
        entity_id=other,
    )
    await _add_card(
        mk,
        org_id,
        currency="USD",
        limit="400.00",
        rebate=("3.00", "confirmed"),
        period=period,
    )

    res = await _dashboard(realdb, org_id, entity_id=None)
    assert res.rebates_ytd == Decimal("10.00")


async def test_every_rebate_aggregate_carries_the_entity_scope(realdb):
    """Structural: a new rebate aggregate must not be added unscoped.

    The three rollups were introduced at different times and only the currency
    predicate was applied uniformly; this asserts the scope is uniform too, so
    a fourth figure cannot reintroduce the org-wide read.
    """
    import ast
    import inspect
    import textwrap

    from app.api import cards as cards_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(cards_mod.card_dashboard)))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef))

    # Statement-granular, not Call-granular: `apply_entity_scope(select(...))`
    # means the inner `select` legitimately carries no scope of its own, so the
    # unit that must show one is the whole statement building the aggregate.
    rebate_aggregates = 0
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.stmt):
            continue
        if any(isinstance(child, ast.stmt) for child in ast.iter_child_nodes(stmt)):
            continue  # a compound statement; its leaves are checked instead
        rendered = ast.dump(stmt)
        if "CardRebate" not in rendered:
            continue
        if not any(f in rendered for f in ("'sum'", "'count'")):
            continue
        rebate_aggregates += 1
        assert "apply_entity_scope" in rendered, (
            "a CardRebate aggregate in card_dashboard is not entity-scoped; "
            "CardRebate has no entity_id of its own, so scope it through the "
            "VirtualCard join the currency predicate already requires"
        )
    assert rebate_aggregates >= 3, (
        f"expected the month, YTD and exclusion-count aggregates; found {rebate_aggregates}"
    )
