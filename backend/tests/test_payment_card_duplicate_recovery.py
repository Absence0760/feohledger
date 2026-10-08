"""The payment-run virtual-card leg must survive a duplicate live card.

``uq_virtual_cards_one_live_per_invoice`` (migration 0067) is the DB backstop
that stops an invoice ever holding two LIVE virtual cards. The batch endpoint
``POST /api/cards/generate`` handles a trip of that index gracefully — it
pre-checks for an already-carded invoice and wraps the flush in a SAVEPOINT
(``app/api/cards.py``). The single-payment leg inside ``_execute_single_payment``
used to do NEITHER: it called the provider unconditionally and flushed the new
row bare.

Two consequences, both reachable today:

  1. *Sequential* — an invoice carded via ``POST /api/cards/generate`` and then
     included in a ``virtual_card`` payment run. The leg minted a SECOND
     provider card (real, spendable) and then failed to persist it: the
     IntegrityError left the row orphaned at the provider.
  2. *Either case* — the IntegrityError poisoned the enclosing transaction. The
     loop's ``except Exception`` set ``payment.status = "failed"``, but the very
     next statement (``dispatch_audit``) and the per-payment ``db.commit()`` run
     on a session that now demands a rollback, so ``PendingRollbackError``
     escaped ``_dispatch_run_payments`` entirely. The run never rolled up, stayed
     ``executing``, and the failed status never persisted — so a ``/resume`` just
     re-drove the same payment into the same crash. Permanently stuck.

These tests drive the real ``execute_payment_run`` against a live Postgres so
the partial unique index actually fires.
"""

from __future__ import annotations

import contextlib
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.virtual_card import VirtualCard
from app.models.workflow import AuditLog

pytestmark = pytest.mark.asyncio

TENANT = "a"


def _user(uid: uuid.UUID):
    return SimpleNamespace(id=uid, full_name="Card Tester", roles=["admin"])


def _org(org_id: uuid.UUID):
    return SimpleNamespace(
        id=org_id,
        name="PyTest",
        slug="pytesta",
        settings={
            "payments": {"provider": "mock"},
            # BYOK + explicit provider keeps the dispatcher on the in-process
            # mock adapter (no network, no credential) — local-first.
            "cards": {
                "enabled": True,
                "program_type": "byok",
                "provider": "mock",
                "region": "US",
                "api_key": "test",
            },
        },
    )


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed(mk, org_id: uuid.UUID, *, amount: Decimal):
    """Approved invoice + a real (screenable) vendor. Returns a detached
    snapshot so nothing lazy-loads against a closed NullPool connection."""
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
                invoice_number=f"CARD-{uuid.uuid4().hex[:8]}",
                vendor_name="Card Vendor",
                vendor_id=vendor_id,
                amount=amount,
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                correlation_id=corr,
            )
        )
        await s.commit()
    return SimpleNamespace(id=inv_id, correlation_id=corr, vendor_id=vendor_id, entity_id=ent)


def _card(inv, org_id: uuid.UUID, *, provider_card_id: str, amount: Decimal) -> VirtualCard:
    return VirtualCard(
        invoice_id=inv.id,
        organization_id=org_id,
        entity_id=inv.entity_id,
        vendor_id=inv.vendor_id,
        correlation_id=inv.correlation_id,
        card_provider="mock",
        provider_card_id=provider_card_id,
        last_four="9999",
        amount_limit=amount,
        currency="USD",
        status="created",
    )


async def _seed_run(mk, org_id, inv, *, amount: Decimal, creator_id) -> tuple[uuid.UUID, uuid.UUID]:
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


@contextlib.contextmanager
def _ambient_patches(*extra):
    """Silence the surrounding machinery (invoice transition, ERP sync,
    sanctions) so each test is about the card leg only."""
    with contextlib.ExitStack() as stack:
        for ctx in (
            patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
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


# ---------------------------------------------------------------------------
# 1. Sequential: the invoice already holds a live card before the run executes
# ---------------------------------------------------------------------------


async def test_pre_existing_live_card_does_not_break_the_payment_run(realdb):
    """An invoice carded earlier (e.g. via POST /api/cards/generate) must not
    mint a second provider card, must not poison the transaction, and must
    leave the run rolled up rather than stuck in `executing`."""
    from app.api.payments import execute_payment_run

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("100.00")

    inv = await _seed(mk, org_id, amount=amount)
    async with mk() as s:
        s.add(_card(inv, org_id, provider_card_id="card_pre_existing", amount=amount))
        await s.commit()

    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )

    create_card_calls = 0
    real_create = None

    from app.services.card_adapters.mock_adapter import MockCardAdapter

    real_create = MockCardAdapter.create_card

    async def _spy(self, payload):
        nonlocal create_card_calls
        create_card_calls += 1
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

    # No second provider card was minted — the leg saw the live one first.
    assert create_card_calls == 0, "provider was called even though a live card exists"

    async with mk() as s:
        n_cards = (
            await s.execute(
                select(func.count())
                .select_from(VirtualCard)
                .where(VirtualCard.invoice_id == inv.id, VirtualCard.status != "cancelled")
            )
        ).scalar_one()
        assert n_cards == 1

        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        run = (await s.execute(select(PaymentRun).where(PaymentRun.id == run_id))).scalar_one()
        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.entity_id == pay_id))).scalars().all()
        )

    # The payment converged on the card that already pays this invoice.
    assert pay.status == "completed"
    assert pay.reference and "9999" in pay.reference
    # The run rolled up — not left stranded in `executing`.
    assert run.status == "completed"
    assert run.executed_at is not None
    assert res["status"] == "completed"
    # Append-only audit trail survived (it is written after the card leg).
    assert any(a.action == "payment.completed" for a in audits)


# ---------------------------------------------------------------------------
# 2. Concurrent: a racing insert wins between our pre-check and our flush
# ---------------------------------------------------------------------------


async def test_racing_card_insert_is_contained_and_the_run_still_rolls_up(realdb):
    """A competing writer commits the invoice's live card AFTER our pre-check
    but BEFORE our flush. The unique index trips; the savepoint must contain it
    so the audit row and the per-payment commit still succeed."""
    from app.api.payments import execute_payment_run
    from app.services.card_issuance import CardIssueResult

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("250.00")

    inv = await _seed(mk, org_id, amount=amount)
    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )

    racer_mk = realdb.sessionmaker(TENANT)

    async def _issue_and_lose_the_race(**kwargs):
        # Simulates the other racer committing first: same invoice, same
        # provider card (both racers send the same idempotency key), committed
        # on an independent connection while we are mid-flight.
        async with racer_mk() as other:
            other.add(_card(inv, org_id, provider_card_id="card_racer_won", amount=amount))
            await other.commit()
        return CardIssueResult(
            card=_card(inv, org_id, provider_card_id="card_racer_won", amount=amount),
            success=True,
        )

    with _ambient_patches(
        patch("app.services.card_issuance.issue_card_for_invoice", _issue_and_lose_the_race)
    ):
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
        n_cards = (
            await s.execute(
                select(func.count())
                .select_from(VirtualCard)
                .where(VirtualCard.invoice_id == inv.id, VirtualCard.status != "cancelled")
            )
        ).scalar_one()
        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        run = (await s.execute(select(PaymentRun).where(PaymentRun.id == run_id))).scalar_one()
        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.entity_id == pay_id))).scalars().all()
        )

    # Exactly one live card survived — the racer's. Ours was never persisted.
    assert n_cards == 1
    assert pay.status == "completed"
    assert run.status == "completed"
    assert run.executed_at is not None
    assert res["status"] == "completed"
    assert any(a.action == "payment.completed" for a in audits)


# ---------------------------------------------------------------------------
# 3. The batch leg: the same savepoint, which never actually contained anything
# ---------------------------------------------------------------------------


async def test_batch_generate_survives_a_racing_card_insert(realdb):
    """`POST /api/cards/generate` claimed to contain a duplicate in a savepoint,
    but added the row to the session BEFORE opening the nested block — and
    `SessionTransaction._take_snapshot` flushes on a `begin_nested()` boundary,
    so the INSERT was issued *before* the SAVEPOINT existed and its
    IntegrityError escaped the very block meant to catch it. The batch then
    aborted on the next statement, discarding sibling cards already minted at
    the provider. Two invoices, the first one raced: the second must still be
    minted and the request must still commit."""
    from app.api.cards import generate_cards
    from app.schemas.virtual_card import GenerateCardsRequest
    from app.services.card_issuance import CardIssueResult

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("75.00")

    raced = await _seed(mk, org_id, amount=amount)
    sibling = await _seed(mk, org_id, amount=amount)

    racer_mk = realdb.sessionmaker(TENANT)

    async def _issue(*, invoice, **kwargs):
        if invoice.id == raced.id:
            async with racer_mk() as other:
                other.add(_card(raced, org_id, provider_card_id="batch_racer", amount=amount))
                await other.commit()
            return CardIssueResult(
                card=_card(raced, org_id, provider_card_id="batch_racer", amount=amount),
                success=True,
            )
        return CardIssueResult(
            card=_card(sibling, org_id, provider_card_id="batch_sibling", amount=amount),
            success=True,
        )

    with _ambient_patches(patch("app.services.card_issuance.issue_card_for_invoice", _issue)):
        async with realdb.sessionmaker(TENANT)() as db:
            res = await generate_cards(
                body=GenerateCardsRequest(invoice_ids=[str(raced.id), str(sibling.id)]),
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                org_id=org_id,
                entity_id=None,
            )
            await db.commit()

    # The raced invoice was skipped; the sibling's card survived the batch.
    assert res.total == 1
    async with mk() as s:
        for inv, provider_card_id in ((raced, "batch_racer"), (sibling, "batch_sibling")):
            cards = (
                (
                    await s.execute(
                        select(VirtualCard).where(
                            VirtualCard.invoice_id == inv.id,
                            VirtualCard.status != "cancelled",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [c.provider_card_id for c in cards] == [provider_card_id]


# ---------------------------------------------------------------------------
# 4. Convergence must never settle against a card whose funds already moved
# ---------------------------------------------------------------------------


async def test_spent_card_is_never_converged_onto(realdb):
    """The reachable operator flow: mint a card → the vendor redeems it (webhook
    flips it to `charged`) → AP voids that payment → the invoice returns to the
    payable pool → a new run creates a fresh `virtual_card` Payment.

    `amount_limit` is the authorization ceiling and is NOT reduced by spend
    (only `amount_charged` is set), so a limit-only guard happily marks the new
    payment `completed` against a card whose money already moved under the
    voided payment. It must fail instead — no second payment, no false
    settlement.
    """
    from app.api.payments import execute_payment_run

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("100.00")

    inv = await _seed(mk, org_id, amount=amount)
    async with mk() as s:
        spent = _card(inv, org_id, provider_card_id="card_spent", amount=amount)
        spent.status = "charged"  # vendor redeemed it; funds moved
        spent.amount_charged = amount
        s.add(spent)
        await s.commit()

    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )

    with _ambient_patches():
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
        cards = (
            (await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == inv.id)))
            .scalars()
            .all()
        )

    assert pay.status == "failed"
    assert pay.failure_reason == "card_already_charged"
    assert pay.completed_at is not None
    # No second card was minted behind the spent one.
    assert [c.provider_card_id for c in cards] == ["card_spent"]
    # The run rolls up honestly rather than reporting a payment that never moved.
    assert res["status"] == "failed"


async def test_converged_payment_is_linked_to_the_card_it_settled_against(realdb):
    """`list_payments` outer-joins the card via `VirtualCard.payment_id ==
    Payment.id`. A payment that converges onto a card minted by
    `POST /api/cards/generate` (which sets no payment_id) must claim that link,
    or the UI shows no card on a payment whose reference says `CARD-…`."""
    from app.api.payments import execute_payment_run

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("100.00")

    inv = await _seed(mk, org_id, amount=amount)
    async with mk() as s:
        s.add(_card(inv, org_id, provider_card_id="card_from_generate", amount=amount))
        await s.commit()

    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )

    with _ambient_patches():
        async with realdb.sessionmaker(TENANT)() as db:
            await execute_payment_run(
                run_id=run_id,
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                entity_id=None,
            )
            await db.commit()

    async with mk() as s:
        card = (
            await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == inv.id))
        ).scalar_one()
        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.entity_id == card.id))).scalars().all()
        )

    assert pay.status == "completed"
    assert card.payment_id == pay.id
    # The reuse is explicit in the SOX trail, and PII-free.
    reuse = [a for a in audits if a.action == "card.reused"]
    assert len(reuse) == 1
    assert reuse[0].details["payment_id"] == str(pay.id)
    assert reuse[0].details["amount"] == "100.00"


async def test_payment_run_mint_writes_a_card_generated_audit_row(realdb):
    """A card-lifecycle query (entity_type=virtual_card, entity_id=card.id) must
    show a creation event for a payment-run-minted card, exactly as it does for
    one minted by the batch endpoint."""
    from app.api.payments import execute_payment_run

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("100.00")

    inv = await _seed(mk, org_id, amount=amount)
    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )

    with _ambient_patches():
        async with realdb.sessionmaker(TENANT)() as db:
            await execute_payment_run(
                run_id=run_id,
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                entity_id=None,
            )
            await db.commit()

    async with mk() as s:
        card = (
            await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == inv.id))
        ).scalar_one()
        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.entity_id == card.id))).scalars().all()
        )

    assert card.payment_id == pay_id  # minted for this payment
    generated = [a for a in audits if a.action == "card.generated"]
    assert len(generated) == 1
    assert generated[0].details["invoice_id"] == str(inv.id)
    assert generated[0].entity_type == "virtual_card"
    # PII guard: the trail carries the last four, never a PAN.
    assert generated[0].details["last_four"] == "4242"


# ---------------------------------------------------------------------------
# 5. Voiding a card payment must kill the card, not just our books
# ---------------------------------------------------------------------------


async def test_void_cancels_an_unspent_card_so_it_cannot_be_rediscovered(realdb):
    """A voided card payment left the card live and spendable at the provider
    with no payment behind it — and still occupying the invoice's live-card
    slot, so the next run rediscovered it. The void now closes it at the
    provider first, then marks the row cancelled + audits."""
    from app.api.payments import VoidPaymentRequest, void_payment

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("100.00")

    inv = await _seed(mk, org_id, amount=amount)
    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )
    async with mk() as s:
        card = _card(inv, org_id, provider_card_id="card_to_void", amount=amount)
        card.payment_id = pay_id
        s.add(card)
        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        pay.status = "completed"
        await s.commit()
        card_id = card.id

    with _ambient_patches():
        async with realdb.sessionmaker(TENANT)() as db:
            void_result = await void_payment(
                payment_id=pay_id,
                body=VoidPaymentRequest(reason="duplicate run"),
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                entity_id=None,
            )

    # The card-cancel outcome is on the RESPONSE now, not only the audit row —
    # the void dialog needs it to tell the operator whether to retry the close.
    assert void_result.void_card_outcome == "card_cancelled"

    async with mk() as s:
        card = (await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))).scalar_one()
        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        void_audit = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == pay_id, AuditLog.action == "payment.voided"
                    )
                )
            )
            .scalars()
            .all()
        )
        card_audit = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == card_id, AuditLog.action == "card.cancelled"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert pay.status == "voided"
    assert card.status == "cancelled"
    assert len(card_audit) == 1
    assert card_audit[0].details["via"] == "payment_void"
    assert void_audit[0].details["card_outcome"] == "card_cancelled"

    # And because the slot is now free, a fresh run mints a NEW card rather
    # than converging onto the dead one.
    async with mk() as s:
        from app.services.card_issuance import find_live_card_for_invoice

        assert await find_live_card_for_invoice(s, inv.id) is None


async def test_void_records_that_an_already_spent_card_could_not_be_cancelled(realdb):
    """A charged card cannot be un-spent — the provider refuses, and so does
    `POST /api/cards/{id}/cancel`. The void must still succeed (the accounting
    intent stands) and record `card_already_charged` for AP to chase, which is
    exactly the state `card_settlement_block` then refuses to settle against."""
    from app.api.payments import VoidPaymentRequest, void_payment

    info = realdb.info(TENANT)
    org_id = info.org_id
    mk = realdb.sessionmaker(TENANT)
    amount = Decimal("100.00")

    inv = await _seed(mk, org_id, amount=amount)
    run_id, pay_id = await _seed_run(
        mk, org_id, inv, amount=amount, creator_id=info.users["ap_manager"]
    )
    async with mk() as s:
        card = _card(inv, org_id, provider_card_id="card_spent_void", amount=amount)
        card.payment_id = pay_id
        card.status = "charged"
        card.amount_charged = amount
        s.add(card)
        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        pay.status = "completed"
        await s.commit()
        card_id = card.id

    with _ambient_patches():
        async with realdb.sessionmaker(TENANT)() as db:
            await void_payment(
                payment_id=pay_id,
                body=VoidPaymentRequest(reason="vendor returned goods"),
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                entity_id=None,
            )

    async with mk() as s:
        card = (await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))).scalar_one()
        pay = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        void_audit = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == pay_id, AuditLog.action == "payment.voided"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert pay.status == "voided"
    assert card.status == "charged"  # untouched — we cannot un-spend it
    assert void_audit[0].details["card_outcome"] == "card_already_charged"
