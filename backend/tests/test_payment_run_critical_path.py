"""Critical-path tests for the money-moving payment flow.

This file fills the genuine gaps left by the existing payment suite,
focused on the highest-risk client flow — the payment run lifecycle and
the CFO sign-off gate. The sibling files already cover:

  - `test_payment_run_flow.py`  — execute run rollups, field hydration,
    the already-completed (409) and CFO-unapproved (403) execute guards.
  - `test_payment_run_actions.py` — void / cancel / approve *guard*
    rejections (404 / 409).
  - `test_payment_webhook_security.py` — webhook HMAC + dedup + terminal
    no-downgrade.

What's NOT covered there, and lives here:

  1. `create_payment_run` — entirely untested. The CFO-threshold
     computation (sets `requires_cfo_approval` by total vs the org's
     `payments.cfo_approval_above`), the malformed-threshold fail-CLOSED
     (a corrupted threshold must require CFO sign-off, never silently
     disable the gate), 404 on a missing invoice, and the Decimal total
     summation are all money-path invariants with no coverage.
  2. `approve_payment_run` happy path — only the guard rejections are
     tested; the actual CFO sign-off (stamps `cfo_approved_at`, writes
     the `payment_run.cfo_approved` audit row) is not.
  3. The end-to-end CFO gate: a run created above the threshold is
     blocked at /execute (403), and only after /approve does /execute
     proceed — i.e. the gate is enforced *before* the adapter is called,
     so no money moves on an un-approved over-threshold run.
  4. Execute writes an audit row on the invoice status change (via
     `transition_invoice`, never a bare `invoice.status = ...` assign) —
     the append-only-audit invariant for the settle path.

All tests are unit-level against the real handler coroutines with mocked
AsyncSessions, matching the established pattern in the sibling files.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.invoice import InvoiceStatus
from app.models.payment import PaymentRun
from app.services.payment_adapters import PaymentStatus

# ---------------------------------------------------------------------------
# Shared fakes (mirror the sibling payment tests' shapes)
# ---------------------------------------------------------------------------


def _user(role: str = "admin"):
    return SimpleNamespace(id=uuid.uuid4(), full_name="Tester", roles=[role])


def _org(*, cfo_above=None, provider: str = "mock"):
    payments: dict = {"provider": provider}
    if cfo_above is not None:
        payments["cfo_approval_above"] = cfo_above
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme",
        settings={"payments": payments},
    )


def _invoice(
    *,
    amount: Decimal,
    status=InvoiceStatus.approved,
    currency: str = "USD",
    reporting_currency: str | None = "USD",
    reporting_fx_rate: Decimal | None = Decimal("1"),
):
    """A stand-in for one `Invoice` row.

    The reporting-currency lock (`reporting_currency` / `reporting_source_currency`
    / `reporting_fx_rate`, materialized by
    `currency_conversion.materialize_reporting_amount` on every save) is part of
    the row the CFO-threshold gate reads: the threshold is denominated in the
    org's REPORTING currency, so a foreign-currency payable has to be expressed
    there before it can be compared. Defaults describe a plain USD invoice on a
    USD-reporting org — rate 1, so the gate sees exactly the face amount.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=None,
        status=status,
        invoice_number="INV-1",
        vendor_name="Acme Corp",
        vendor_id=None,
        currency=currency,
        description=None,
        amount=amount,
        reporting_currency=reporting_currency,
        reporting_source_currency=currency,
        reporting_fx_rate=reporting_fx_rate,
    )


def _create_run_db(
    invoices: list,
    blocking_invoice_ids: list | None = None,
    credit_totals: dict | None = None,
    card_claimed_invoice_ids: list | None = None,
    live_payment_invoice_ids: list | None = None,
):
    """Build an AsyncSession mock for `create_payment_run`.

    The handler issues, IN THIS ORDER: (1) `select(Invoice).where(Invoice.id
    .in_(...))` → `.scalars().all()`, (2) `net_payable_amounts` → ONE grouped
    already-applied-credit-memo SUM over every invoice → `.all()` of
    `(invoice_id, total)`, then `run_refusal_reasons`' own three:
    (3) the unresolved payment-blocking exception gate → `.all()` of
    `(invoice_id, exception_type)` rows, (4) the live-virtual-card claim gate
    → `.scalars().all()` of invoice ids, (5) the live-payment backstop →
    `.all()` of `(invoice_id, invoice_number)`. It then `db.add(run)`,
    `db.flush()`, `db.add(payment)` per item, and `db.commit()`. We
    capture every added object so a test can inspect the created run +
    payment rows. Finally, AFTER the commit, (7) `_run_currencies` reads the
    one currency the run's invoices share → `.all()` of `(run_id, code)` — the
    handler names it on the response instead of stamping a `$` on the total.

    The sequence is positional (`side_effect` is a list), so a NEW query in
    the handler shifts every later result and fails these tests loudly —
    which is the point. Mirror the real order here rather than making the
    handler defensive.

    (2)-(5) used to be four separate hand-written gates in the handler, with
    one credit SUM per item; they are now the single `run_refusal_reasons`
    predicate set the payment queue also reads, over one grouped credit query.

    `blocking_invoice_ids` seeds (3) so a test can simulate an invoice sitting
    under an open duplicate/fraud exception. `card_claimed_invoice_ids` seeds
    (4), for an invoice already holding a live virtual card
    (`POST /api/cards/generate` mints one with no Payment row behind it).
    `live_payment_invoice_ids` seeds (5) — the pre-check that now names those
    invoices before the insert instead of leaving it to the
    `uq_payments_one_live_per_invoice` IntegrityError. `credit_totals` (keyed by
    invoice id) seeds (2); an invoice not present defaults to no applied credit,
    which preserves every existing test's un-netted totals.
    """
    sel = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=invoices)
    sel.scalars = MagicMock(return_value=scalars)

    # The gate query selects TWO columns — `(invoice_id, exception_type)` — and
    # `payment_runs.blocking_exception_types` consumes them with `rows.all()`,
    # NOT `.scalars().all()`. Seeding only the scalars shape leaves `rows.all()`
    # answering with a bare MagicMock, which iterates empty, so the gate sees no
    # blocked invoice and this test silently stops testing anything.
    block_sel = MagicMock()
    block_sel.all = MagicMock(
        return_value=[(inv_id, "duplicate") for inv_id in (blocking_invoice_ids or [])]
    )

    # The live-card claim gate: `.scalars().all()` of the invoice ids already
    # occupied by a non-cancelled VirtualCard.
    card_sel = MagicMock()
    card_scalars = MagicMock()
    card_scalars.all = MagicMock(return_value=list(card_claimed_invoice_ids or []))
    card_sel.scalars = MagicMock(return_value=card_scalars)

    # The live-payment backstop: `.all()` of `(invoice_id, invoice_number)`,
    # which `live_payment_invoices` turns into a dict.
    by_id = {inv.id: inv for inv in invoices}
    live_sel = MagicMock()
    live_sel.all = MagicMock(
        return_value=[
            (inv_id, getattr(by_id.get(inv_id), "invoice_number", None))
            for inv_id in (live_payment_invoice_ids or [])
        ]
    )

    # One grouped credit-memo SUM for the whole batch, not one per invoice.
    credit_totals = credit_totals or {}
    credit_sel = MagicMock()
    credit_sel.all = MagicMock(
        return_value=[
            (inv.id, credit_totals[inv.id]) for inv in invoices if inv.id in credit_totals
        ]
    )

    # (6) `applied_credit_conflicts`, the last of `run_refusal_reasons`' reads:
    # `.all()` of `(invoice_id, vendor_id, currency)` for the applied memos. The
    # credit totals above are seeded as bare sums with no memo rows behind them,
    # so no pairing exists to break.
    memo_pair_sel = MagicMock()
    memo_pair_sel.all = MagicMock(return_value=[])

    db = AsyncMock()

    # (7) `_run_currencies`, after the commit. Resolved lazily off `db.added`
    # because the run's id is minted by the handler, not by this builder — a
    # fixed row would key against a run that doesn't exist and the currency
    # would read `None` for the wrong reason.
    currency_sel = MagicMock()
    currency_sel.all = MagicMock(
        side_effect=lambda: (
            [
                (obj.id, (invoices[0].currency or "").upper() or None)
                for obj in db.added
                if isinstance(obj, PaymentRun)
            ]
            if invoices
            else []
        )
    )

    db.execute = AsyncMock(
        side_effect=[sel, credit_sel, block_sel, card_sel, live_sel, memo_pair_sel, currency_sel]
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.added = []
    db.add = MagicMock(side_effect=lambda obj: db.added.append(obj))
    # The run + payment inserts run inside a savepoint (`db.begin_nested()`) so
    # the session stays usable after an IntegrityError and can name the invoices
    # already holding a live payment. A bare AsyncMock returns a coroutine here,
    # not an async context manager — model the real session.
    db.begin_nested = MagicMock(side_effect=lambda: _null_async_cm())
    return db


@contextlib.asynccontextmanager
async def _null_async_cm():
    yield


# ---------------------------------------------------------------------------
# create_payment_run — the untested entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_rejects_an_unapproved_invoice():
    """A run may only be built from approved (payable) invoices. Including a
    `new`/`rejected`/etc. invoice — which the executor would then move real
    money for — is refused with 409, and nothing is committed. (The handler
    used to only check existence despite a 'are payable' comment.)"""
    from fastapi import HTTPException

    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("100.00"), status=InvoiceStatus.new)
    body = CreatePaymentRunRequest(
        items=[CreatePaymentRunItem(invoice_id=str(inv.id), method="ach")]
    )
    db = _create_run_db([inv])

    with pytest.raises(HTTPException) as exc:
        await create_payment_run(
            body=body, db=db, org=_org(), user=_user(), org_id=uuid.uuid4(), entity_id=uuid.uuid4()
        )
    assert exc.value.status_code == 409
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_run_rejects_invoice_with_unresolved_duplicate_exception():
    """An approved invoice that still carries an OPEN `duplicate` (or
    `fraud_flag`) exception must not enter a payment run — otherwise a
    same-invoice duplicate could be approved and paid a second time (a real
    double-payment; the duplicate warning is advisory and doesn't block on its
    own). A human clears it by resolving/dismissing the exception. Refused with
    409; nothing committed."""
    from fastapi import HTTPException

    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("100.00"), status=InvoiceStatus.approved)
    body = CreatePaymentRunRequest(
        items=[CreatePaymentRunItem(invoice_id=str(inv.id), method="ach")]
    )
    # Invoice is payable by status but sits under an unresolved duplicate flag.
    db = _create_run_db([inv], blocking_invoice_ids=[inv.id])

    with pytest.raises(HTTPException) as exc:
        await create_payment_run(
            body=body, db=db, org=_org(), user=_user(), org_id=uuid.uuid4(), entity_id=uuid.uuid4()
        )
    assert exc.value.status_code == 409
    assert "duplicate" in exc.value.detail.lower()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_run_rejects_invoice_already_holding_a_live_virtual_card():
    """`POST /api/cards/generate` mints a spendable card and books NO `Payment`,
    so nothing that keys on payment rows can see the claim and the invoice stays
    payable by ACH — the vendor gets a card AND a wire. Refused on every rail
    except `virtual_card`, which converges on the existing card. Realdb coverage
    (including the converge and cancelled-card exits) is in
    `test_payment_run_live_card_claim.py`; this pins the run builder's own
    branch."""
    from fastapi import HTTPException

    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("1000.00"))
    body = CreatePaymentRunRequest(
        items=[CreatePaymentRunItem(invoice_id=str(inv.id), method="ach")]
    )
    db = _create_run_db([inv], card_claimed_invoice_ids=[inv.id])

    with pytest.raises(HTTPException) as exc:
        await create_payment_run(
            body=body, db=db, org=_org(), user=_user(), org_id=uuid.uuid4(), entity_id=uuid.uuid4()
        )
    assert exc.value.status_code == 409
    assert "virtual card" in exc.value.detail.lower()
    assert inv.invoice_number in exc.value.detail
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_run_allows_invoice_whose_duplicate_exception_is_cleared():
    """The gate keys on UNRESOLVED exceptions only. Once a human resolves or
    dismisses the duplicate/fraud exception (the sign-off), the invoice is
    payable again — the blocking query returns no rows and the run builds."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("100.00"), status=InvoiceStatus.approved)
    body = CreatePaymentRunRequest(
        items=[CreatePaymentRunItem(invoice_id=str(inv.id), method="ach")]
    )
    # No unresolved blocking exceptions (dismissed/resolved ones don't match).
    db = _create_run_db([inv], blocking_invoice_ids=[])

    result = await create_payment_run(
        body=body, db=db, org=_org(), user=_user(), org_id=uuid.uuid4(), entity_id=uuid.uuid4()
    )
    assert result["payment_count"] == 1
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_standalone_payment_rejects_an_unapproved_invoice():
    """POST /api/payments records a payment against an invoice; it must refuse
    a pre-approval invoice (booking money against something nobody signed off)."""
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from app.api.payments import create_payment
    from app.schemas.payment import PaymentCreate

    inv = _invoice(amount=Decimal("100.00"), status=InvoiceStatus.rejected)
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=inv)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    db.flush = AsyncMock()

    body = PaymentCreate(invoice_id=str(inv.id), amount=Decimal("100.00"), method="ach")
    with pytest.raises(HTTPException) as exc:
        await create_payment(body=body, db=db, user=_user())
    assert exc.value.status_code == 409
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_run_404_when_an_invoice_is_missing():
    """If any selected invoice id doesn't resolve, the whole run is
    refused with 404 — we never create a partial run that silently drops
    invoices the operator thought they were paying."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("100.00"))
    # Request names TWO invoices; the DB returns only ONE.
    body = CreatePaymentRunRequest(
        items=[
            CreatePaymentRunItem(invoice_id=str(inv.id)),
            CreatePaymentRunItem(invoice_id=str(uuid.uuid4())),
        ]
    )
    db = _create_run_db([inv])

    with pytest.raises(HTTPException) as exc:
        await create_payment_run(
            body=body,
            db=db,
            org=_org(),
            user=_user(),
            org_id=uuid.uuid4(),
            entity_id=uuid.uuid4(),
        )
    assert exc.value.status_code == 404
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_create_run_sums_total_as_decimal_and_creates_pending_payments():
    """The run's `total_amount` must be the exact Decimal sum of the
    invoice amounts (no float drift), and each child Payment lands in
    `pending` with its OWN freshly-minted correlation_id.

    correlation_id is the per-payment idempotency anchor sent to the rail
    as the Idempotency-Key — it must be unique per payment attempt, never
    copied from the invoice (a shared key makes a re-queued-after-void
    payment reuse the original order and silently skip moving money). The
    webhook + reconciler join on provider_payment_id, not correlation_id."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    # Amounts chosen so a float sum would drift: 0.1 + 0.2 != 0.3 in float.
    i1 = _invoice(amount=Decimal("0.10"))
    i2 = _invoice(amount=Decimal("0.20"))
    body = CreatePaymentRunRequest(
        items=[
            CreatePaymentRunItem(invoice_id=str(i1.id), method="ach"),
            CreatePaymentRunItem(invoice_id=str(i2.id), method="wire"),
        ]
    )
    db = _create_run_db([i1, i2])

    result = await create_payment_run(
        body=body,
        db=db,
        org=_org(),  # no CFO threshold configured
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    runs = [o for o in db.added if isinstance(o, PaymentRun)]
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "draft"
    assert run.total_amount == Decimal("0.30")
    assert isinstance(run.total_amount, Decimal)
    assert run.requires_cfo_approval is False

    payments = [o for o in db.added if o.__class__.__name__ == "Payment"]
    assert len(payments) == 2
    assert {p.method for p in payments} == {"ach", "wire"}
    assert all(p.status == "pending" for p in payments)
    # Each payment gets its OWN freshly-minted correlation_id — never the
    # invoice's (a shared rail Idempotency-Key would mask a re-pay as settled).
    corr_ids = [p.correlation_id for p in payments]
    assert len(set(corr_ids)) == 2, "each payment must get a distinct correlation_id"
    assert i1.correlation_id not in corr_ids
    assert i2.correlation_id not in corr_ids

    assert result["requires_cfo_approval"] is False
    assert result["payment_count"] == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_run_serialises_total_as_exact_decimal_string_not_float():
    """Money invariant on the wire: the `create_payment_run` JSON response
    encodes `total_amount` as an EXACT Decimal STRING, never a float. A float
    hop is forbidden even where `Numeric(15, 2)` currently loses no precision —
    the string is the contract the frontend coerces at its arithmetic sites."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("100.00"))
    body = CreatePaymentRunRequest(
        items=[CreatePaymentRunItem(invoice_id=str(inv.id), method="ach")]
    )
    db = _create_run_db([inv])

    result = await create_payment_run(
        body=body, db=db, org=_org(), user=_user(), org_id=uuid.uuid4(), entity_id=uuid.uuid4()
    )

    assert result["total_amount"] == "100.00"
    assert isinstance(result["total_amount"], str)
    assert not isinstance(result["total_amount"], float)


@pytest.mark.asyncio
async def test_get_run_detail_serialises_money_as_strings_not_floats():
    """The run-detail JSON (`GET /api/payments/runs/{id}`, a raw dict, not a
    Pydantic schema) must encode both the run `total_amount` and every child
    payment `amount` as exact Decimal STRINGS — the same money-on-the-wire
    invariant as the run-create response. Feeds RunDetailModal."""
    from app.api.payments import get_payment_run

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="draft",
        total_amount=Decimal("250.00"),
        initiated_by=uuid.uuid4(),
        executed_at=None,
        created_at=datetime.now(UTC),
        requires_cfo_approval=False,
        cfo_approved_by=None,
        cfo_approved_at=None,
    )
    pay = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        amount=Decimal("250.00"),
        method="ach",
        status="pending",
        reference=None,
        provider=None,
        failure_reason=None,
        retry_of_payment_id=None,
        submitted_at=None,
        completed_at=None,
    )
    inv = _invoice(amount=Decimal("250.00"))

    run_res = MagicMock()
    run_res.scalar_one_or_none = MagicMock(return_value=run)
    pay_res = MagicMock()
    pay_res.all = MagicMock(return_value=[(pay, inv)])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[run_res, pay_res])

    result = await get_payment_run(run_id=run.id, db=db, user=_user())

    assert result["total_amount"] == "250.00"
    assert isinstance(result["total_amount"], str)
    assert result["payments"][0]["amount"] == "250.00"
    assert isinstance(result["payments"][0]["amount"], str)


@pytest.mark.asyncio
async def test_create_run_writes_creation_audit_row():
    """Assembling a payment run must write a `payment_run.created` audit row —
    the SOX trail for a run begins at assembly, not execution. Without it, an
    insider who builds a fraudulent run and cancels it before /execute leaves no
    record of who assembled it. PII-free: only ids, total (Decimal string), and
    counts in the details payload."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("250.00"))
    body = CreatePaymentRunRequest(
        items=[CreatePaymentRunItem(invoice_id=str(inv.id), method="ach")]
    )
    db = _create_run_db([inv])
    org = _org()

    with patch("app.services.audit_dispatch.dispatch_audit", new_callable=AsyncMock) as mk_audit:
        await create_payment_run(
            body=body,
            db=db,
            org=org,
            user=_user(),
            org_id=uuid.uuid4(),
            entity_id=uuid.uuid4(),
        )

    mk_audit.assert_awaited_once()
    kwargs = mk_audit.call_args.kwargs
    assert kwargs["action"] == "payment_run.created"
    assert kwargs["entity_type"] == "payment_run"
    assert kwargs["organization_id"] == org.id
    assert kwargs["details"]["total_amount"] == "250.00"
    assert kwargs["details"]["payment_count"] == 1
    # The audit row is committed in the same transaction as the run.
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_run_twice_for_same_invoice_yields_distinct_idempotency_keys():
    """The void → re-queue → re-pay regression: building a second payment run
    for the same invoice (after the first payment was voided) must produce a
    Payment with a correlation_id distinct from the first run's. correlation_id
    is the rail Idempotency-Key; if both runs shared the invoice's stable id the
    processor would return the cached first order and the vendor would never be
    re-paid, yet AP would record a settled payment."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("100.00"))
    body = CreatePaymentRunRequest(
        items=[CreatePaymentRunItem(invoice_id=str(inv.id), method="ach")]
    )

    db1 = _create_run_db([inv])
    await create_payment_run(
        body=body, db=db1, org=_org(), user=_user(), org_id=uuid.uuid4(), entity_id=uuid.uuid4()
    )
    first = [o for o in db1.added if o.__class__.__name__ == "Payment"][0]

    db2 = _create_run_db([inv])
    await create_payment_run(
        body=body, db=db2, org=_org(), user=_user(), org_id=uuid.uuid4(), entity_id=uuid.uuid4()
    )
    second = [o for o in db2.added if o.__class__.__name__ == "Payment"][0]

    assert first.correlation_id != second.correlation_id, (
        "a re-run for the same invoice must get a fresh idempotency key"
    )


@pytest.mark.asyncio
async def test_create_run_flags_cfo_approval_above_threshold():
    """A run whose total clears the org's `cfo_approval_above` threshold
    must be created with `requires_cfo_approval=True`. This is the gate
    that later blocks /execute until a CFO signs off."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("10000.00"))
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    result = await create_payment_run(
        body=body,
        db=db,
        org=_org(cfo_above="5000"),  # 10000 > 5000 → gate trips
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    run = next(o for o in db.added if isinstance(o, PaymentRun))
    assert run.requires_cfo_approval is True
    assert result["requires_cfo_approval"] is True
    assert "CFO approval required" in result["message"]


@pytest.mark.asyncio
async def test_create_run_does_not_flag_cfo_when_total_at_or_below_threshold():
    """The threshold is strict-greater-than: a total exactly equal to the
    threshold does NOT require CFO approval (boundary check)."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("5000.00"))
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    await create_payment_run(
        body=body,
        db=db,
        org=_org(cfo_above="5000"),  # total == threshold → no gate
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    run = next(o for o in db.added if isinstance(o, PaymentRun))
    assert run.requires_cfo_approval is False


@pytest.mark.asyncio
async def test_create_run_fails_closed_on_malformed_threshold():
    """A typo'd `cfo_approval_above` (non-numeric) must not blow up run
    creation — but it must fail *CLOSED*, not open. A configured-but-
    unparseable CFO gate that silently disabled itself let a single
    settings write turn a fraud control off for every run (an insider
    could corrupt the value on purpose). The handler now creates the run
    *requiring* CFO approval and logs the misconfig, rather than 500-ing
    every run (which would halt payments org-wide) or skipping the gate."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("99999.00"))
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    # Should not raise — a settings typo can't halt all payments.
    result = await create_payment_run(
        body=body,
        db=db,
        org=_org(cfo_above="not-a-number"),
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    run = next(o for o in db.added if isinstance(o, PaymentRun))
    # Fail-closed: the un-parseable gate REQUIRES CFO sign-off.
    assert run.requires_cfo_approval is True
    assert result["requires_cfo_approval"] is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_run_ignores_zero_or_negative_threshold():
    """A threshold of 0 (or negative) means "no gate configured" — it
    must NOT trip CFO approval on every run. The handler only gates when
    the threshold is strictly positive."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    inv = _invoice(amount=Decimal("1000000.00"))
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    await create_payment_run(
        body=body,
        db=db,
        org=_org(cfo_above="0"),
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    run = next(o for o in db.added if isinstance(o, PaymentRun))
    assert run.requires_cfo_approval is False


# ---------------------------------------------------------------------------
# The CFO threshold is denominated in the org's REPORTING currency
#
# `payments.cfo_approval_above` is a bare number, exactly like
# `expense_approval.cfo_threshold`, and the org's reporting currency is what it
# is denominated in. Comparing it against the run's own-currency total made the
# gate fail OPEN on every foreign-currency payable priced below the threshold in
# its own units — a GBP 9,000 run (USD 11,400) executed with no CFO sign-off.
# Refusing a *mixed*-currency run, which the builder already did, closes the
# batch half of that hole and none of this one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_gates_cfo_on_the_reporting_currency_figure():
    """A foreign-currency run worth MORE than the threshold once expressed in
    the org's reporting currency requires CFO sign-off, even though its
    face-value total is below the threshold."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    # GBP 9,000 at the rate locked on the invoice row = USD 11,400.
    inv = _invoice(
        amount=Decimal("9000.00"),
        currency="GBP",
        reporting_currency="USD",
        reporting_fx_rate=Decimal("1.26666667"),
    )
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    result = await create_payment_run(
        body=body,
        db=db,
        org=_org(cfo_above="10000"),
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    run = next(o for o in db.added if isinstance(o, PaymentRun))
    assert run.requires_cfo_approval is True
    assert result["requires_cfo_approval"] is True
    # The run still PAYS 9,000 GBP — only the gate's comparison changed.
    assert result["total_amount"] == "9000.00"


@pytest.mark.asyncio
async def test_create_run_does_not_over_fire_on_a_small_foreign_total():
    """The fix is a conversion, not a blanket "foreign currency ⇒ CFO". A
    JPY 1,000,000 run is USD 6,700 at its locked rate, comfortably under a USD
    10,000 threshold, and must NOT require sign-off — otherwise every non-USD
    tenant would need a CFO for every payment."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    inv = _invoice(
        amount=Decimal("1000000.00"),
        currency="JPY",
        reporting_currency="USD",
        reporting_fx_rate=Decimal("0.00670000"),
    )
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    await create_payment_run(
        body=body,
        db=db,
        org=_org(cfo_above="10000"),
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    run = next(o for o in db.added if isinstance(o, PaymentRun))
    assert run.requires_cfo_approval is False


@pytest.mark.asyncio
async def test_create_run_fails_closed_when_the_reporting_figure_is_unknowable():
    """A foreign invoice carrying no rate we can prove describes its currency
    pair can't be compared against the threshold at all. Treat it as OVER —
    the same posture `services/expense_currency` takes for a report whose
    reporting figure is unavailable — never as under."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        PaymentRun,
        create_payment_run,
    )

    inv = _invoice(
        amount=Decimal("100.00"),
        currency="EUR",
        reporting_currency=None,
        reporting_fx_rate=None,
    )
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    await create_payment_run(
        body=body,
        db=db,
        org=_org(cfo_above="10000"),
        user=_user(),
        org_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
    )

    run = next(o for o in db.added if isinstance(o, PaymentRun))
    assert run.requires_cfo_approval is True


@pytest.mark.asyncio
async def test_create_run_audit_records_what_the_cfo_gate_compared():
    """For a foreign run the gated figure is NOT `total_amount`, so the audit
    row has to carry it — otherwise nothing explains why a GBP 9,000 run needed
    sign-off against a 10,000 threshold. PII-free: amounts, a currency code and
    a fixed reason vocabulary."""
    from app.api.payments import (
        CreatePaymentRunItem,
        CreatePaymentRunRequest,
        create_payment_run,
    )

    inv = _invoice(
        amount=Decimal("9000.00"),
        currency="GBP",
        reporting_currency="USD",
        reporting_fx_rate=Decimal("1.26666667"),
    )
    body = CreatePaymentRunRequest(items=[CreatePaymentRunItem(invoice_id=str(inv.id))])
    db = _create_run_db([inv])

    with patch("app.services.audit_dispatch.dispatch_audit", new_callable=AsyncMock) as da:
        await create_payment_run(
            body=body,
            db=db,
            org=_org(cfo_above="10000"),
            user=_user(),
            org_id=uuid.uuid4(),
            entity_id=uuid.uuid4(),
        )

    details = da.await_args.kwargs["details"]
    assert details["cfo_threshold_currency"] == "USD"
    assert details["cfo_evaluated_amount"] == "11400.00"
    assert details["cfo_reason"] == "above_threshold"
    # Exact decimal strings, never floats.
    assert isinstance(details["cfo_evaluated_amount"], str)


# ---------------------------------------------------------------------------
# approve_payment_run — the happy-path sign-off (only guards tested before)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_run_stamps_cfo_and_writes_audit():
    """The CFO sign-off must stamp `cfo_approved_by` / `cfo_approved_at`
    AND write a `payment_run.cfo_approved` audit row (the append-only
    audit invariant — a money-authorization decision is regulated)."""
    from app.api.payments import approve_payment_run

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="draft",
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=True,
        cfo_approved_at=None,
        cfo_approved_by=None,
        total_amount=Decimal("10000.00"),
    )
    sel = MagicMock()
    sel.scalar_one_or_none = MagicMock(return_value=run)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=sel)
    db.commit = AsyncMock()

    cfo = _user(role="cfo")
    before = datetime.now(UTC)
    with patch("app.services.audit_dispatch.dispatch_audit", new_callable=AsyncMock) as da:
        result = await approve_payment_run(run_id=run.id, db=db, org=_org(), user=cfo)

    assert run.cfo_approved_by == cfo.id
    assert run.cfo_approved_at is not None
    assert run.cfo_approved_at >= before
    # Status stays draft — approval is a pre-execute gate, not an execute.
    assert run.status == "draft"

    da.assert_awaited_once()
    audit_kwargs = da.call_args.kwargs
    assert audit_kwargs["action"] == "payment_run.cfo_approved"
    assert audit_kwargs["entity_type"] == "payment_run"
    assert audit_kwargs["entity_id"] == run.id
    assert audit_kwargs["actor_id"] == cfo.id

    assert result["cfo_approved_by"] == str(cfo.id)
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# End-to-end CFO gate: blocked before approve, allowed after — and the
# adapter (= money movement) is never reached while un-approved.
# ---------------------------------------------------------------------------


def _execute_db(run, payments, invoice_by_id, vendor_by_invoice=None, completing_payment_ids=None):
    """DB mock for `execute_payment_run`: run SELECT, pending-payments
    SELECT, then per-payment invoice SELECT — and, for any invoice carrying a
    `vendor_id`, the compliance gate's follow-on Vendor SELECT — then the
    final rollup SELECT over every payment on the run. Mirrors `_mock_db` in
    test_payment_run_flow.py but inline so this file stays standalone.

    `completing_payment_ids` (default: none) names payments the caller expects
    to reach a `completed` adapter result — `_execute_single_payment` follows
    that with `_capture_discount_offers`'s own `DiscountOffer` lookup (issue
    #280), so those get one more mocked (empty-scalars) result appended after
    their vendor lookup. A payment that holds/fails before the adapter call
    (e.g. the no-screenable-vendor compliance hold) never reaches it — leave
    such payments out of the set."""
    vendor_by_invoice = vendor_by_invoice or {}
    completing_payment_ids = completing_payment_ids or set()
    run_result = MagicMock()
    run_result.scalar_one_or_none = MagicMock(return_value=run)

    payments_result = MagicMock()
    payments_scalars = MagicMock()
    payments_scalars.all = MagicMock(return_value=payments)
    payments_result.scalars = MagicMock(return_value=payments_scalars)

    per_pay_results: list = []
    for p in payments:
        inv = invoice_by_id.get(str(p.invoice_id))
        inv_res = MagicMock()
        inv_res.scalar_one_or_none = MagicMock(return_value=inv)
        per_pay_results.append(inv_res)
        if inv is not None:
            # `_execute_single_payment` re-runs two shared gates right after the
            # payable-status re-check, before pricing: (1) `blocking_exception_types`
            # — a `fraud_flag` raised after the run was built (an approved BEC
            # bank swap) must stop dispatch; (2) `card_claimed_invoice_ids` →
            # `live_card_invoice_ids` — a card minted after the run claims the
            # invoice. Model both as empty (not blocked, no card). A `virtual_card`
            # payment skips (2) — these tests all use `ach`.
            blocking_res = MagicMock()
            blocking_res.all = MagicMock(return_value=[])
            per_pay_results.append(blocking_res)
            card_claim_res = MagicMock()
            card_claim_scalars = MagicMock()
            card_claim_scalars.all = MagicMock(return_value=[])
            card_claim_res.scalars = MagicMock(return_value=card_claim_scalars)
            per_pay_results.append(card_claim_res)
            # `applied_credit_integrity.applied_credit_conflicts` — the applied
            # memos' vendor/currency pairing, re-checked before pricing
            # (decisions §214). No applied memo here, so no conflict.
            memo_pair_res = MagicMock()
            memo_pair_res.all = MagicMock(return_value=[])
            per_pay_results.append(memo_pair_res)
            # `_execute_single_payment` re-derives the invoice's net payable
            # (invoice amount − applied credit memos) immediately before the
            # adapter call, so a credit recorded after the run was built can't
            # pay the stale figure. Model that SUM here: no credits applied.
            # Only fires when the invoice resolved — mirrors the guard in
            # `_execute_single_payment`.
            credit_res = MagicMock()
            credit_res.scalar_one = MagicMock(return_value=Decimal("0"))
            per_pay_results.append(credit_res)
        # For any invoice with a vendor_id the executor issues two follow-on
        # SELECTs, in order: (1) the vendor's bank_details (for the payload /
        # intl-leg detection) and (2) the full Vendor row for the compliance
        # gate. Interleave both so the side_effect list stays in lockstep.
        if inv is not None and getattr(inv, "vendor_id", None):
            bank_res = MagicMock()
            bank_res.scalar_one_or_none = MagicMock(return_value=None)  # domestic, no intl
            per_pay_results.append(bank_res)
            ven_res = MagicMock()
            ven_res.scalar_one_or_none = MagicMock(
                return_value=vendor_by_invoice.get(str(p.invoice_id))
            )
            per_pay_results.append(ven_res)
        if p.id in completing_payment_ids:
            discount_res = MagicMock()
            discount_scalars = MagicMock()
            discount_scalars.all = MagicMock(return_value=[])
            discount_res.scalars = MagicMock(return_value=discount_scalars)
            per_pay_results.append(discount_res)

    rollup_result = MagicMock()
    rollup_scalars = MagicMock()
    rollup_scalars.all = MagicMock(return_value=payments)
    rollup_result.scalars = MagicMock(return_value=rollup_scalars)

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[run_result, payments_result, *per_pay_results, rollup_result]
    )
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


def _clear_compliance():
    """A compliance decision that neither refuses nor holds — the payment may
    proceed to the adapter."""
    return SimpleNamespace(verdict="allow", reasons=[])


def _payment(amount: Decimal = Decimal("10000.00")):
    return SimpleNamespace(
        id=uuid.uuid4(),
        payment_run_id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        amount=amount,
        method="ach",
        status="pending",
        provider=None,
        provider_payment_id=None,
        reference=None,
        submitted_at=None,
        completed_at=None,
        failure_reason=None,
        retry_of_payment_id=None,
        correlation_id=uuid.uuid4(),
        source_currency=None,
        source_amount=None,
        fx_rate=None,
        fx_locked_at=None,
        corridor=None,
        target_country=None,
    )


def _adapter():
    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="px_1",
            reference="REF-1",
            failure_reason=None,
        )
    )
    return adapter


@pytest.mark.asyncio
async def test_over_threshold_run_blocks_execute_then_proceeds_after_cfo_approval():
    """The full CFO gate, end to end:

      1. An over-threshold run (`requires_cfo_approval=True`,
         `cfo_approved_at=None`) is refused at /execute with 403, and
         critically the payment adapter is NEVER called — money cannot
         move on an un-approved run.
      2. After the CFO signs off (`cfo_approved_at` set), the SAME run
         executes: the adapter is called and the payment settles.

    This is the load-bearing assertion: the gate is enforced *before*
    the adapter, not cosmetically after.
    """
    from app.api.payments import execute_payment_run

    run_id = uuid.uuid4()

    # --- Phase 1: un-approved → 403, no adapter call -----------------
    blocked_run = SimpleNamespace(
        id=run_id,
        status="draft",
        total_amount=Decimal("10000.00"),
        organization_id=uuid.uuid4(),
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=True,
        cfo_approved_at=None,
        cfo_approved_by=None,
        executed_at=None,
    )
    sel = MagicMock()
    sel.scalar_one_or_none = MagicMock(return_value=blocked_run)
    blocked_db = AsyncMock()
    blocked_db.execute = AsyncMock(return_value=sel)

    with (
        patch("app.api.payments.get_payment_adapter") as mk_adapter,
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()) as mk_sync,
    ):
        with pytest.raises(HTTPException) as exc:
            await execute_payment_run(
                run_id=run_id, db=blocked_db, org=_org(cfo_above="5000"), user=_user()
            )

    assert exc.value.status_code == 403
    mk_adapter.assert_not_called()  # MONEY DID NOT MOVE
    mk_sync.assert_not_called()
    assert blocked_run.executed_at is None

    # --- Phase 2: CFO-approved → executes, adapter called ------------
    approved_run = SimpleNamespace(
        id=run_id,
        status="draft",
        total_amount=Decimal("10000.00"),
        organization_id=uuid.uuid4(),
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=True,
        cfo_approved_at=datetime.now(UTC),
        cfo_approved_by=uuid.uuid4(),
        executed_at=None,
    )
    p = _payment()
    inv = _invoice(amount=Decimal("10000.00"))
    inv.vendor_id = uuid.uuid4()
    vendor = SimpleNamespace(id=inv.vendor_id, name="Acme Corp")
    invoices = {str(p.invoice_id): inv}
    exec_db = _execute_db(
        approved_run,
        [p],
        invoices,
        vendor_by_invoice={str(p.invoice_id): vendor},
        completing_payment_ids={p.id},
    )
    adapter = _adapter()

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.api.payments.transition_invoice", new_callable=AsyncMock) as ti,
        patch(
            "app.services.compliance.check_payment_compliance",
            new_callable=AsyncMock,
            return_value=_clear_compliance(),
        ),
    ):
        ti.return_value = invoices[str(p.invoice_id)]
        result = await execute_payment_run(
            run_id=run_id, db=exec_db, org=_org(cfo_above="5000"), user=_user()
        )

    adapter.create_payment.assert_awaited_once()  # money moved exactly once
    assert approved_run.status == "completed"
    assert approved_run.executed_at is not None
    assert result["payments_completed"] == 1
    assert p.status == "completed"


# ---------------------------------------------------------------------------
# Execute writes an audit row for the invoice status change (append-only
# audit invariant — the settle path goes through transition_invoice, never
# a bare `invoice.status = ...` assignment).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_transitions_invoice_via_transition_invoice_not_bare_assign():
    """A completed payment must flip its invoice to `payment_scheduled`
    *through* `transition_invoice` (which writes the audit row), not by
    assigning `invoice.status` directly. A regression that assigned the
    status field would mutate state with no audit trail — an Improvement-
    to-Critical violation of invariant #3 on a regulated money event."""
    from app.api.payments import execute_payment_run

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="draft",
        total_amount=Decimal("100.00"),
        organization_id=uuid.uuid4(),
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=False,
        cfo_approved_at=None,
        cfo_approved_by=None,
        executed_at=None,
    )
    p = _payment(amount=Decimal("100.00"))
    invoice = _invoice(amount=Decimal("100.00"), status=InvoiceStatus.approved)
    invoice.vendor_id = uuid.uuid4()
    vendor = SimpleNamespace(id=invoice.vendor_id, name="Acme Corp")
    invoices = {str(p.invoice_id): invoice}
    db = _execute_db(
        run,
        [p],
        invoices,
        vendor_by_invoice={str(p.invoice_id): vendor},
        completing_payment_ids={p.id},
    )
    adapter = _adapter()

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.api.payments.transition_invoice", new_callable=AsyncMock) as ti,
        patch(
            "app.services.compliance.check_payment_compliance",
            new_callable=AsyncMock,
            return_value=_clear_compliance(),
        ),
    ):
        ti.return_value = invoice
        await execute_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    ti.assert_awaited_once()
    # Positional contract: transition_invoice(db, invoice, target_status, ...)
    assert ti.call_args.args[2] == InvoiceStatus.payment_scheduled
    assert ti.call_args.kwargs.get("action_name") == "invoice.payment_scheduled"


# ---------------------------------------------------------------------------
# Compliance gate: an invoice with no screenable vendor must NEVER be paid
# unscreened — it holds for AP. (NULL vendor_id was a silent screening bypass.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_holds_payment_when_invoice_has_no_screenable_vendor():
    """An invoice that reached a run with `vendor_id=None` (e.g. AI-extracted /
    email-intake that never matched a vendor) cannot be sanctions-screened. The
    executor MUST hold it (`pending_compliance`) rather than pay an unscreened
    payee — the adapter is never called and the invoice never transitions."""
    from app.api.payments import execute_payment_run

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="draft",
        total_amount=Decimal("100.00"),
        organization_id=uuid.uuid4(),
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=False,
        cfo_approved_at=None,
        cfo_approved_by=None,
        executed_at=None,
    )
    p = _payment(amount=Decimal("100.00"))
    invoice = _invoice(amount=Decimal("100.00"), status=InvoiceStatus.approved)
    invoice.vendor_id = None  # the bypass surface
    db = _execute_db(run, [p], {str(p.invoice_id): invoice})
    # The hold now also opens an Exception (payment_compliance_hold) so it's
    # surfaced in the queue — that's an extra dedupe-check SELECT ("does an
    # open one already exist?") between the invoice lookup and the final
    # rollup query. Splice a "none found" result into that slot.
    no_existing_exception = MagicMock()
    no_existing_exception.scalar_one_or_none = MagicMock(return_value=None)
    side_effects = list(db.execute.side_effect)
    side_effects.insert(-1, no_existing_exception)
    db.execute = AsyncMock(side_effect=side_effects)
    adapter = _adapter()

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.api.payments.transition_invoice", new_callable=AsyncMock) as ti,
    ):
        result = await execute_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    adapter.create_payment.assert_not_called()  # money did NOT move
    ti.assert_not_awaited()  # invoice did NOT transition
    assert p.status == "pending_compliance"
    assert "no screenable vendor" in (p.failure_reason or "")
    assert result["payments_completed"] == 0
