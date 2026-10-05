"""`GET /api/payments/queue` must say which rows a payment run would refuse.

`services/payment_runs.create_payment_run_for_invoices` rejects the WHOLE run
with a 409 when ANY selected invoice carries an unresolved
(`open`/`escalated`) exception in `PAYMENT_BLOCKING_EXCEPTION_TYPES`. The queue
offered those rows anyway, so selecting one — with nothing on screen marking it
— produced a hard failure of the entire draft and no indication of which row
caused it.

The queue now resolves `blocked` / `blocked_reason` through the SAME
`payment_runs` helper the run builder uses, so the two can't drift and a new
blocking type updates both surfaces at once. `blocked_reason` is the exception
TYPE only: it is rendered to an operator and travels through a JSON body, and a
description can carry vendor / bank / amount detail.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.payments import PAYABLE_INVOICE_STATUSES
from app.models.exception import Exception as ExceptionModel
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment

pytestmark = pytest.mark.asyncio

TENANT = "a"

# Description text that must never reach the response: the whole point of
# returning a TYPE rather than the exception row is that a description can
# carry vendor / bank / amount detail.
SENSITIVE_DESCRIPTION = "Bank account 1234567890 for Acme Ltd changed by supplier email"


def _user(uid):
    return SimpleNamespace(id=uid, full_name="Queue Tester", roles=["admin"])


def _org(org_id):
    return SimpleNamespace(id=org_id, name="PyTest", slug="pytesta", settings={})


async def _seed_invoice(
    mk,
    org_id,
    *,
    number: str,
    amount: str = "500.00",
    status: InvoiceStatus = InvoiceStatus.approved,
) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=number,
                vendor_name="Queue Vendor",
                amount=Decimal(amount),
                currency="USD",
                status=status,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return inv_id


async def _add_exception(
    mk,
    org_id,
    invoice_id: uuid.UUID,
    *,
    exception_type: str,
    status: str = "open",
    description: str | None = None,
) -> None:
    async with mk() as s:
        s.add(
            ExceptionModel(
                id=uuid.uuid4(),
                invoice_id=invoice_id,
                exception_type=exception_type,
                severity="error",
                status=status,
                description=description,
                organization_id=org_id,
            )
        )
        await s.commit()


async def _book_payment(mk, invoice_id: uuid.UUID, *, status: str = "submitted") -> None:
    async with mk() as s:
        s.add(
            Payment(
                invoice_id=invoice_id,
                amount=Decimal("500.00"),
                method="ach",
                status=status,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()


async def _queue_result(realdb, mk):
    from app.api.payments import payment_queue

    info = realdb.info(TENANT)
    async with mk() as db:
        return await payment_queue(
            db=db, org=_org(info.org_id), user=_user(info.users["admin"]), entity_id=None
        )


async def _queue(realdb, mk):
    result = await _queue_result(realdb, mk)
    return {item["invoice_number"]: item for item in result["items"]}


async def test_unflagged_row_is_not_blocked(realdb):
    mk = realdb.sessionmaker(TENANT)
    await _seed_invoice(mk, realdb.info(TENANT).org_id, number="Q-CLEAN")

    rows = await _queue(realdb, mk)
    assert rows["Q-CLEAN"]["blocked"] is False
    assert rows["Q-CLEAN"]["blocked_reason"] is None


async def test_open_blocking_exception_marks_the_row(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-DUP")
    await _add_exception(mk, org_id, inv_id, exception_type="duplicate")

    rows = await _queue(realdb, mk)
    assert rows["Q-DUP"]["blocked"] is True
    assert rows["Q-DUP"]["blocked_reason"] == "duplicate"


async def test_non_blocking_exception_does_not_mark_the_row(realdb):
    """`po_mismatch` is a real exception the queue must keep offering — a run
    does not refuse it, so marking the row would make an payable invoice
    unpayable from the UI."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-POMM")
    await _add_exception(mk, org_id, inv_id, exception_type="po_mismatch")

    rows = await _queue(realdb, mk)
    assert rows["Q-POMM"]["blocked"] is False
    assert rows["Q-POMM"]["blocked_reason"] is None


async def test_escalated_blocking_exception_still_blocks(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-ESC")
    await _add_exception(mk, org_id, inv_id, exception_type="fraud_flag", status="escalated")

    rows = await _queue(realdb, mk)
    assert rows["Q-ESC"]["blocked"] is True
    assert rows["Q-ESC"]["blocked_reason"] == "fraud_flag"


@pytest.mark.parametrize("resolution", ["resolved", "dismissed"])
async def test_signed_off_exception_releases_the_row(realdb, resolution):
    """Resolving / dismissing IS the human sign-off — the run builder accepts
    the invoice again, so the queue must too."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number=f"Q-SIGNED-{resolution}")
    await _add_exception(mk, org_id, inv_id, exception_type="duplicate", status=resolution)

    rows = await _queue(realdb, mk)
    assert rows[f"Q-SIGNED-{resolution}"]["blocked"] is False


async def test_payment_reconciliation_blocks_without_the_queue_restating_the_tuple(realdb):
    """`payment_reconciliation` was added to `PAYMENT_BLOCKING_EXCEPTION_TYPES`
    after the queue was written. It blocks here only because the queue IMPORTS
    the tuple instead of restating it — this test fails the moment someone
    inlines a literal list of types."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES

    assert "payment_reconciliation" in PAYMENT_BLOCKING_EXCEPTION_TYPES

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-RECON")
    await _add_exception(mk, org_id, inv_id, exception_type="payment_reconciliation")

    rows = await _queue(realdb, mk)
    assert rows["Q-RECON"]["blocked"] is True
    assert rows["Q-RECON"]["blocked_reason"] == "payment_reconciliation"


async def test_every_blocking_type_is_reported(realdb):
    """Whatever the tuple holds, each member must actually block — so adding a
    type can't leave the queue silently offering rows a run will refuse."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    for exception_type in PAYMENT_BLOCKING_EXCEPTION_TYPES:
        inv_id = await _seed_invoice(mk, org_id, number=f"Q-ALL-{exception_type}")
        await _add_exception(mk, org_id, inv_id, exception_type=exception_type)

    rows = await _queue(realdb, mk)
    for exception_type in PAYMENT_BLOCKING_EXCEPTION_TYPES:
        row = rows[f"Q-ALL-{exception_type}"]
        assert row["blocked"] is True, exception_type
        assert row["blocked_reason"] == exception_type, exception_type


async def test_reason_is_deterministic_when_several_exceptions_are_open(realdb):
    """Two blocking exceptions on one invoice must not make the reason depend
    on row order — the answer is the earliest member of the tuple."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-MULTI")
    await _add_exception(mk, org_id, inv_id, exception_type="fraud_flag")
    await _add_exception(mk, org_id, inv_id, exception_type="duplicate")

    first = min(("fraud_flag", "duplicate"), key=PAYMENT_BLOCKING_EXCEPTION_TYPES.index)
    for _ in range(3):
        rows = await _queue(realdb, mk)
        assert rows["Q-MULTI"]["blocked_reason"] == first


async def test_blocked_reason_never_carries_the_exception_description(realdb):
    """PII invariant: the reason is a fixed vocabulary code, never the
    exception's free text (which can name a bank account or a vendor)."""
    import json

    from app.api.payments import payment_queue

    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="Q-PII")
    await _add_exception(
        mk,
        info.org_id,
        inv_id,
        exception_type="fraud_flag",
        description=SENSITIVE_DESCRIPTION,
    )

    async with mk() as db:
        result = await payment_queue(
            db=db, org=_org(info.org_id), user=_user(info.users["admin"]), entity_id=None
        )

    payload = json.dumps(result)
    assert "1234567890" not in payload
    assert SENSITIVE_DESCRIPTION not in payload
    row = next(i for i in result["items"] if i["invoice_number"] == "Q-PII")
    assert row["blocked_reason"] == "fraud_flag"


async def test_queue_blocked_set_matches_the_run_builders_own_verdict(realdb):
    """Drift guard: what the queue marks blocked is exactly what
    `payment_runs.blocked_invoice_ids` (the run builder's gate) refuses."""
    from app.api.payments import payment_queue
    from app.services.payment_runs import blocked_invoice_ids

    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    blocked_id = await _seed_invoice(mk, info.org_id, number="Q-DRIFT-BLOCKED")
    clean_id = await _seed_invoice(mk, info.org_id, number="Q-DRIFT-CLEAN")
    await _add_exception(mk, info.org_id, blocked_id, exception_type="line_total_mismatch")

    async with mk() as db:
        result = await payment_queue(
            db=db, org=_org(info.org_id), user=_user(info.users["admin"]), entity_id=None
        )
        builder_verdict = await blocked_invoice_ids(db, [blocked_id, clean_id])

    queue_verdict = {uuid.UUID(i["id"]) for i in result["items"] if i["blocked"]}
    assert queue_verdict == builder_verdict == {blocked_id}


@pytest.mark.parametrize(
    "live_status", ["submitted", "processing", "pending", "pending_compliance"]
)
async def test_an_invoice_with_a_live_payment_is_excluded_not_offered(realdb, live_status):
    """The queue used to exclude only `completed` payments, so an invoice with a
    `submitted` payment (any real rail — ACH settles in 1-3 days) was a
    selectable queue row. `create_payment_run_for_invoices` then hard-409s it on
    `uq_payments_one_live_per_invoice`, taking the whole select-all batch down.
    It must not appear in the queue, `/queue/ids`, or the selectable count."""
    from app.api.payments import payment_queue_ids

    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    live_id = await _seed_invoice(mk, info.org_id, number=f"Q-LIVE-{live_status}")
    clean_id = await _seed_invoice(mk, info.org_id, number=f"Q-FREE-{live_status}")
    await _book_payment(mk, live_id, status=live_status)

    result = await _queue_result(realdb, mk)
    offered = {i["invoice_number"] for i in result["items"]}
    assert f"Q-LIVE-{live_status}" not in offered
    assert f"Q-FREE-{live_status}" in offered

    async with mk() as db:
        ids_resp = await payment_queue_ids(
            db=db, org=_org(info.org_id), user=_user(info.users["admin"]), entity_id=None
        )
    resolved = set(ids_resp["ids"])
    assert str(live_id) not in resolved
    assert str(clean_id) in resolved


async def test_a_terminal_failed_payment_does_not_exclude_the_invoice(realdb):
    """The exclusion is LIVE payments only — a `failed` / `voided` / `cancelled`
    payment is outside `uq_payments_one_live_per_invoice`, so the run builder
    accepts the invoice and the queue must keep offering it (re-pay after a
    failure)."""
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="Q-RETRY")
    await _book_payment(mk, inv_id, status="failed")

    rows = await _queue(realdb, mk)
    assert "Q-RETRY" in rows


async def test_queue_offered_set_carries_nothing_the_run_builder_would_refuse(realdb):
    """The invariant: the SELECTABLE set the queue resolves for "select all N"
    carries nothing either of the run builder's own refusal predicates would
    reject — `blocked_invoice_ids` (blocking exceptions) and
    `_live_payment_invoice_numbers` (the `uq_payments_one_live_per_invoice`
    guard). Seeds a clean invoice, a blocked-by-exception one and a
    live-payment one."""
    from app.api.payments import payment_queue_ids
    from app.services.payment_runs import _live_payment_invoice_numbers, blocked_invoice_ids

    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    clean_id = await _seed_invoice(mk, info.org_id, number="Q-INV-CLEAN")
    exc_id = await _seed_invoice(mk, info.org_id, number="Q-INV-EXC")
    live_id = await _seed_invoice(mk, info.org_id, number="Q-INV-LIVE")
    await _add_exception(mk, info.org_id, exc_id, exception_type="fraud_flag")
    await _book_payment(mk, live_id, status="submitted")

    all_three = [clean_id, exc_id, live_id]
    async with mk() as db:
        ids_resp = await payment_queue_ids(
            db=db, org=_org(info.org_id), user=_user(info.users["admin"]), entity_id=None
        )
        offered = {uuid.UUID(i) for i in ids_resp["ids"]}
        refused_by_exception = await blocked_invoice_ids(db, all_three)
        refused_for_live_payment = set(await _live_payment_invoice_numbers(db, all_three))

    # The run builder would refuse exactly exc_id (exception) and live_id (live
    # payment); the queue offers neither, and offers the clean one.
    assert refused_by_exception == {exc_id}
    assert refused_for_live_payment == {"Q-INV-LIVE"}
    assert clean_id in offered
    assert exc_id not in offered and live_id not in offered


# ---------------------------------------------------------------------------
# The OTHER refusals — everything the run builder enforces, not just exceptions
# ---------------------------------------------------------------------------
#
# Excluding a live payment (above) closed one of four per-invoice refusals.
# `services/payment_runs.run_refusal_reasons` is now the single predicate set
# both surfaces read, so these prove the remaining two reach the queue AND that
# the queue's SQL restatement of them agrees with the Python verdict.


async def _seed_vendor(mk, org_id) -> uuid.UUID:
    from app.models.vendor import Vendor

    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(Vendor(id=vendor_id, organization_id=org_id, name="Queue Vendor"))
        await s.commit()
    return vendor_id


async def _apply_credit(mk, org_id, invoice_id: uuid.UUID, *, amount: str) -> None:
    """An APPLIED credit memo against the invoice — what `net_payable_amounts`
    subtracts. An `open` memo must not count."""
    from sqlalchemy import update

    from app.models.credit_memo import CreditMemo

    vendor_id = await _seed_vendor(mk, org_id)
    async with mk() as s:
        # The API can only apply a credit to an invoice linked to the memo's
        # own vendor (`credit_memos._assert_vendor_matches`), and the payment
        # paths refuse any other pairing (`applied_credit_mismatch`), so the
        # fixture links them the same way.
        await s.execute(update(Invoice).where(Invoice.id == invoice_id).values(vendor_id=vendor_id))
        s.add(
            CreditMemo(
                id=uuid.uuid4(),
                memo_number=f"CM-{uuid.uuid4().hex[:8]}",
                vendor_id=vendor_id,
                invoice_id=invoice_id,
                amount=Decimal(amount),
                currency="USD",
                status="applied",
                organization_id=org_id,
            )
        )
        await s.commit()


async def _mint_card(mk, org_id, invoice_id: uuid.UUID, *, status: str = "created") -> None:
    """A live virtual card claiming the invoice, with no `Payment` behind it —
    exactly what `POST /api/cards/generate` persists."""
    from app.models.virtual_card import VirtualCard

    async with mk() as s:
        s.add(
            VirtualCard(
                organization_id=org_id,
                invoice_id=invoice_id,
                card_provider="mock",
                provider_card_id=f"mock_{uuid.uuid4().hex[:12]}",
                last_four="4242",
                amount_limit=Decimal("500.00"),
                currency="USD",
                status=status,
            )
        )
        await s.commit()


async def _default_entity_id(mk) -> uuid.UUID:
    """The tenant's `is_default` Entity — what `get_write_entity_id` resolves to
    and what a run's `entity_id` FK must point at."""
    from sqlalchemy import select as sa_select

    from app.models.entity import Entity

    async with mk() as s:
        return (
            await s.execute(sa_select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
        ).scalar_one()


async def _selectable_ids(realdb, mk) -> set[str]:
    from app.api.payments import payment_queue_ids

    info = realdb.info(TENANT)
    async with mk() as db:
        resp = await payment_queue_ids(
            db=db, org=_org(info.org_id), user=_user(info.users["admin"]), entity_id=None
        )
    return set(resp["ids"])


async def test_a_fully_credited_invoice_is_blocked_not_offered(realdb):
    """Applied credit memos covering the whole invoice leave nothing to pay, so
    the run builder 409s it. The queue used to offer it anyway — selecting it,
    or select-all, took the entire batch down with no way to bisect."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-CREDITED", amount="500.00")
    await _apply_credit(mk, org_id, inv_id, amount="500.00")

    rows = await _queue(realdb, mk)
    assert rows["Q-CREDITED"]["blocked"] is True
    assert rows["Q-CREDITED"]["blocked_reason"] == "fully_credited"
    assert rows["Q-CREDITED"]["required_method"] is None
    assert str(inv_id) not in await _selectable_ids(realdb, mk)


async def test_a_partly_credited_invoice_stays_payable(realdb):
    """The refusal is "nothing left to move", not "a credit exists" — a partial
    credit still leaves money to pay, so the row stays selectable."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-PARTCREDIT", amount="500.00")
    await _apply_credit(mk, org_id, inv_id, amount="100.00")

    rows = await _queue(realdb, mk)
    assert rows["Q-PARTCREDIT"]["blocked"] is False
    assert rows["Q-PARTCREDIT"]["blocked_reason"] is None
    assert str(inv_id) in await _selectable_ids(realdb, mk)


async def _set_currency(mk, invoice_id: uuid.UUID, currency: str) -> None:
    """A background re-extraction rewriting the currency under an applied credit."""
    from sqlalchemy import update

    async with mk() as s:
        await s.execute(update(Invoice).where(Invoice.id == invoice_id).values(currency=currency))
        await s.commit()


async def test_a_credit_whose_currency_no_longer_pairs_blocks_the_row(realdb):
    """A credit applied in USD to an invoice since re-extracted as EUR would be
    subtracted across currencies, so a run refuses it — and the queue must say
    so rather than offer a row that 409s the batch (decisions §214). Case on
    the invoice side is not a mismatch, as on the apply guard."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    moved = await _seed_invoice(mk, org_id, number="Q-CUR-MOVED")
    same = await _seed_invoice(mk, org_id, number="Q-CUR-SAME")
    await _apply_credit(mk, org_id, moved, amount="100.00")
    await _apply_credit(mk, org_id, same, amount="100.00")
    await _set_currency(mk, moved, "EUR")
    await _set_currency(mk, same, "usd")

    rows = await _queue(realdb, mk)
    assert rows["Q-CUR-MOVED"]["blocked"] is True
    assert rows["Q-CUR-MOVED"]["blocked_reason"] == "applied_credit_mismatch"
    assert rows["Q-CUR-SAME"]["blocked"] is False
    selectable = await _selectable_ids(realdb, mk)
    assert str(moved) not in selectable
    assert str(same) in selectable


async def test_a_card_claimed_invoice_is_rail_pinned_not_blocked(realdb):
    """A live virtual card is refused on every rail EXCEPT `virtual_card`, which
    converges onto that card. So the row is NOT blocked — blocking it would kill
    the documented mint-a-card-then-run-it flow — but it does leave the
    select-all set, which stages the default rail and would 409."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-CARDED")
    await _mint_card(mk, org_id, inv_id)

    rows = await _queue(realdb, mk)
    assert rows["Q-CARDED"]["blocked"] is False
    assert rows["Q-CARDED"]["blocked_reason"] == "live_virtual_card"
    assert rows["Q-CARDED"]["required_method"] == "virtual_card"
    assert str(inv_id) not in await _selectable_ids(realdb, mk)


async def test_a_cancelled_card_releases_the_rail_pin(realdb):
    """`uq_virtual_cards_one_live_per_invoice`'s own predicate is
    `status <> 'cancelled'` — cancelling the card releases the claim, so the row
    goes back to being payable on any rail."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-CARDGONE")
    await _mint_card(mk, org_id, inv_id, status="cancelled")

    rows = await _queue(realdb, mk)
    assert rows["Q-CARDGONE"]["blocked"] is False
    assert rows["Q-CARDGONE"]["required_method"] is None
    assert str(inv_id) in await _selectable_ids(realdb, mk)


async def test_blocked_total_counts_blocked_rows_not_merely_unselectable_ones(realdb):
    """`blocked_total` used to be `total - selectable_total`. Those stopped being
    complementary the moment a rail-CONDITIONAL refusal existed: a card-claimed
    row leaves the selectable set but its checkbox still works, so subtracting
    would report it to the operator as something to go and clear."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    clean_id = await _seed_invoice(mk, org_id, number="Q-TOT-CLEAN")
    exc_id = await _seed_invoice(mk, org_id, number="Q-TOT-EXC")
    card_id = await _seed_invoice(mk, org_id, number="Q-TOT-CARD")
    await _add_exception(mk, org_id, exc_id, exception_type="duplicate")
    await _mint_card(mk, org_id, card_id)

    result = await _queue_result(realdb, mk)
    on_page = {i["invoice_number"]: i for i in result["items"]}
    assert on_page["Q-TOT-EXC"]["blocked"] is True
    assert on_page["Q-TOT-CARD"]["blocked"] is False

    selectable = await _selectable_ids(realdb, mk)
    assert str(clean_id) in selectable
    assert str(exc_id) not in selectable and str(card_id) not in selectable

    # The card row is counted OUT of selectable but NOT into blocked, so the two
    # deliberately do not add up to the total.
    assert result["total"] - result["selectable_total"] > result["blocked_total"]


async def test_the_queues_sql_selectable_set_matches_the_python_verdict(realdb):
    """The drift guard that matters. `/queue/ids` + `selectable_total` restate
    the refusal predicates in SQL (they must, or every whole-set aggregate would
    stream every invoice id into Python); the per-row flags come from
    `run_refusal_reasons`. This compares the two over a population carrying every
    refusal at once — so a SQL clause that stops matching its Python twin fails
    here rather than as a 409 in production."""
    from sqlalchemy import select as sa_select

    from app.models.invoice import Invoice as InvoiceModel
    from app.services.payment_runs import run_refusal_reasons

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    clean_id = await _seed_invoice(mk, org_id, number="Q-SQL-CLEAN")
    exc_id = await _seed_invoice(mk, org_id, number="Q-SQL-EXC")
    credited_id = await _seed_invoice(mk, org_id, number="Q-SQL-CREDIT", amount="200.00")
    card_id = await _seed_invoice(mk, org_id, number="Q-SQL-CARD")
    live_id = await _seed_invoice(mk, org_id, number="Q-SQL-LIVE")
    mismatch_id = await _seed_invoice(mk, org_id, number="Q-SQL-MISMATCH")
    await _add_exception(mk, org_id, exc_id, exception_type="fraud_flag")
    await _apply_credit(mk, org_id, credited_id, amount="200.00")
    await _apply_credit(mk, org_id, mismatch_id, amount="50.00")
    await _set_currency(mk, mismatch_id, "eur")
    await _mint_card(mk, org_id, card_id)
    await _book_payment(mk, live_id, status="submitted")

    selectable = await _selectable_ids(realdb, mk)

    seeded = [clean_id, exc_id, credited_id, card_id, live_id, mismatch_id]
    async with mk() as db:
        invoices = (
            (await db.execute(sa_select(InvoiceModel).where(InvoiceModel.id.in_(seeded))))
            .scalars()
            .all()
        )
        verdicts = await run_refusal_reasons(db, invoices)

    # Python: each refused row names the reason its own seed created.
    assert verdicts[exc_id].reason == "fraud_flag"
    assert verdicts[credited_id].reason == "fully_credited"
    assert verdicts[card_id].reason == "live_virtual_card"
    assert verdicts[live_id].reason == "live_payment"
    assert verdicts[mismatch_id].reason == "applied_credit_mismatch"
    assert clean_id not in verdicts

    # SQL: the selectable set is exactly the rows with no refusal.
    assert str(clean_id) in selectable
    for refused in (exc_id, credited_id, card_id, live_id, mismatch_id):
        assert str(refused) not in selectable, refused


async def test_every_queue_reason_code_is_one_the_run_builder_can_actually_raise():
    """`blocked_reason` is a fixed vocabulary the client localises. Pin it: the
    non-exception codes are exactly `payment_runs`' own reason constants, every
    one of them has an operator-facing 409 message, and the rail-conditional one
    is derived from `CARD_CONVERGING_METHODS` rather than restated."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services import payment_runs

    non_exception = {
        payment_runs.REFUSAL_LIVE_VIRTUAL_CARD,
        payment_runs.REFUSAL_APPLIED_CREDIT_MISMATCH,
        payment_runs.REFUSAL_FULLY_CREDITED,
        payment_runs.REFUSAL_LIVE_PAYMENT,
    }
    assert non_exception.isdisjoint(PAYMENT_BLOCKING_EXCEPTION_TYPES)
    # A reason with no message falls through to the builder's generic refusal.
    assert set(payment_runs._REFUSAL_MESSAGES) == non_exception
    assert set(payment_runs._REFUSAL_ORDER) == non_exception
    # The one rail a card-claimed invoice stays payable on comes FROM the
    # converge set, so the two cannot drift.
    assert payment_runs.CARD_CLAIM_ONLY_METHOD in payment_runs.CARD_CONVERGING_METHODS


async def test_the_run_builder_still_409s_each_refusal_with_its_own_message(realdb):
    """Folding four hand-written gates into one shared predicate set must not
    change which message an operator gets. Each refusal keeps naming the invoice
    and its own cause."""
    from types import SimpleNamespace as NS

    from fastapi import HTTPException

    from app.services.payment_runs import PaymentRunItemInput, create_payment_run_for_invoices

    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    org_id = info.org_id
    credited_id = await _seed_invoice(mk, org_id, number="Q-409-CREDIT", amount="300.00")
    card_id = await _seed_invoice(mk, org_id, number="Q-409-CARD")
    live_id = await _seed_invoice(mk, org_id, number="Q-409-LIVE")
    await _apply_credit(mk, org_id, credited_id, amount="300.00")
    await _mint_card(mk, org_id, card_id)
    await _book_payment(mk, live_id, status="submitted")

    org = _org(org_id)
    user = NS(id=info.users["admin"], full_name="Queue Tester")

    async def _refuse(invoice_id, method="ach"):
        async with mk() as db:
            with pytest.raises(HTTPException) as exc:
                await create_payment_run_for_invoices(
                    db,
                    org=org,
                    org_id=org_id,
                    entity_id=uuid.uuid4(),
                    scope_entity_id=None,
                    user=user,
                    items=[PaymentRunItemInput(invoice_id=invoice_id, method=method)],
                )
        return exc.value

    credited = await _refuse(credited_id)
    assert credited.status_code == 409
    assert "credit" in credited.detail.lower()
    assert "Q-409-CREDIT" in credited.detail

    carded = await _refuse(card_id)
    assert carded.status_code == 409
    assert "card" in carded.detail.lower()
    assert "Q-409-CARD" in carded.detail

    live = await _refuse(live_id)
    assert live.status_code == 409
    assert "live payment" in live.detail.lower()
    assert "Q-409-LIVE" in live.detail


async def test_the_converging_card_rail_is_still_accepted_by_the_builder(realdb):
    """The queue's `required_method` is only honest if the builder really does
    accept that rail — a card-claimed invoice paid BY card converges onto the
    existing card, which is the documented mint-then-run flow."""
    from types import SimpleNamespace as NS

    from app.services.payment_runs import PaymentRunItemInput, create_payment_run_for_invoices

    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    org_id = info.org_id
    inv_id = await _seed_invoice(mk, org_id, number="Q-CONVERGE")
    await _mint_card(mk, org_id, inv_id)

    rows = await _queue(realdb, mk)
    pinned = rows["Q-CONVERGE"]["required_method"]
    assert pinned == "virtual_card"

    # The tenant's REAL default entity — `PaymentRun.entity_id` is an FK, and a
    # made-up id fails the insert as an IntegrityError the builder reads as the
    # live-payment backstop.
    entity_id = await _default_entity_id(mk)
    async with mk() as db:
        result = await create_payment_run_for_invoices(
            db,
            org=_org(org_id),
            org_id=org_id,
            entity_id=entity_id,
            scope_entity_id=None,
            user=NS(id=info.users["admin"], full_name="Queue Tester"),
            items=[PaymentRunItemInput(invoice_id=inv_id, method=pinned)],
        )
        assert result.created is True
        assert result.run.status == "draft"
        await db.rollback()


# ---------------------------------------------------------------------------
# A GENUINELY `submitted` payment, booked by the app's own execute path
# ---------------------------------------------------------------------------


async def test_a_rail_that_reports_submitted_removes_the_invoice_from_the_queue(realdb):
    """The end-to-end reproduction, not a hand-written row.

    The follow-up this closes named the obstacle: the `mock` payment adapter
    returns `completed` synchronously, so no local tenant ever HAS a payment
    sitting in `submitted` — which is why the queue shipped for so long excluding
    only `completed` payments while every real rail (ACH settles in 1-3 days)
    leaves one `submitted` for days. The other tests here insert the row
    directly, which is a faithful reproduction of the DB state the unique index
    acts on; this one proves the state is reachable through
    `execute_payment_run` itself, with a stub standing in for the rail the mock
    adapter cannot imitate.

    The assertion is deliberately narrow. On `submitted` the real code transitions
    the invoice to `payment_scheduled`, which is still in
    `PAYABLE_INVOICE_STATUSES` — so the invoice remains a queue candidate BY
    STATUS and the only thing keeping it out is the live payment. Nothing about
    the transition is patched, precisely so that stays true rather than being
    arranged.
    """
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select as sa_select

    from app.api.payments import execute_payment_run, payment_queue_ids
    from app.models.invoice import Invoice as InvoiceModel
    from app.models.payment import Payment as PaymentModel
    from app.models.payment import PaymentRun
    from app.services.payment_adapters import PaymentStatus

    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    org_id = info.org_id
    admin_id = info.users["admin"]

    inv_id = await _seed_invoice(mk, org_id, number="Q-INFLIGHT", amount="250.00")
    entity_id = await _default_entity_id(mk)
    # A REAL linked vendor is load-bearing, not scenery: with `vendor_id` NULL the
    # ACH leg's own fail-safe parks the payment at `pending_compliance` before the
    # sanctions adapter is ever consulted ("we cannot screen a payee we don't
    # have"), so the patched gate below would never run and the payment would
    # never reach `submitted`.
    vendor_id = await _seed_vendor(mk, org_id)
    async with mk() as s:
        invoice_row = (
            await s.execute(sa_select(InvoiceModel).where(InvoiceModel.id == inv_id))
        ).scalar_one()
        invoice_row.vendor_id = vendor_id
        await s.commit()

    run_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                entity_id=entity_id,
                status="draft",
                total_amount=Decimal("250.00"),
                # A DIFFERENT user, so `check_run_segregation` doesn't refuse the
                # admin executing it.
                initiated_by=uuid.uuid4(),
                requires_cfo_approval=False,
            )
        )
        await s.flush()
        s.add(
            PaymentModel(
                id=uuid.uuid4(),
                invoice_id=inv_id,
                entity_id=entity_id,
                payment_run_id=run_id,
                amount=Decimal("250.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    async def _submitting_create_payment(payload):
        """What a real ACH rail answers: accepted, not settled. The webhook
        finalises it days later."""
        return SimpleNamespace(
            success=True,
            status=PaymentStatus.submitted,
            provider_payment_id="px_inflight",
            reference="REF-INFLIGHT",
            failure_reason=None,
        )

    adapter = SimpleNamespace(
        provider_name="stub_rail",
        create_payment=_submitting_create_payment,
    )

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", new_callable=AsyncMock),
        patch(
            "app.services.compliance.check_payment_compliance",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(verdict="allow", reasons=[]),
        ),
    ):
        async with mk() as db:
            await execute_payment_run(
                run_id=run_id,
                db=db,
                org=_org(org_id),
                user=SimpleNamespace(id=admin_id, full_name="Queue Tester", roles=["admin"]),
                entity_id=None,
            )
            await db.commit()

    async with mk() as s:
        payment = (
            await s.execute(sa_select(PaymentModel).where(PaymentModel.invoice_id == inv_id))
        ).scalar_one()
        invoice = (
            await s.execute(sa_select(InvoiceModel).where(InvoiceModel.id == inv_id))
        ).scalar_one()

    # The state the mock adapter can never produce, produced.
    assert payment.status == "submitted"
    # ...and the invoice is STILL payable by status, so nothing but the live
    # payment can account for what follows.
    invoice_status = getattr(invoice.status, "value", invoice.status)
    assert invoice_status in PAYABLE_INVOICE_STATUSES

    result = await _queue_result(realdb, mk)
    offered = {i["invoice_number"] for i in result["items"]}
    assert "Q-INFLIGHT" not in offered

    async with mk() as db:
        ids_resp = await payment_queue_ids(
            db=db, org=_org(org_id), user=_user(admin_id), entity_id=None
        )
    assert str(inv_id) not in set(ids_resp["ids"])
