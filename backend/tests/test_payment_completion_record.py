"""One recorder for a completed payment — settlement verdict AND realized FX.

There are exactly two paths on which a payment reaches ``completed``: the
processor's webhook (`api/payments.payment_webhook`) and the reconciler
backstop that exists for when that webhook never arrives
(`services/payment_reconciler`). They had already drifted once on the
settlement verdict; the fix factored that into
`services/payment_settlement_record.record_settlement` — and then they drifted
again, one field over.

**Realized FX gain/loss was computed inline in the webhook handler and nowhere
else.** So every cross-currency payment the backstop recovered booked no
realized gain or loss at all — silently, permanently, and on the exact
population with the least evidence by construction. Nothing downstream
re-derives it either: the webhook handler refuses an already-terminal payment,
so a late webhook could not supply the missing figure.

These tests pin both halves of the fix:

* the **behaviour** — a cross-currency payment settled by the backstop carries
  ``details.realized_fx_gain_loss`` on its append-only audit row, at the same
  figure the webhook path books for the same inputs;
* the **drift guard** — `record_completion` is the only place under ``app/``
  that computes realized FX for a settlement, so a third completion path
  cannot reintroduce the gap by copying a block instead of calling the owner.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.payment_adapters import PaymentStatus, SettlementReport

# The webhook leg resolves the processor's webhook secret through the sealed
# store; the fake org's settings stand in for it (see the fixture).
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# The two rows, shared by both paths so the parity assertion is meaningful:
# a 1 000.00 EUR invoice accrued at 1.10 USD/EUR (1 100.00 USD) and settled by
# an outflow of 1 080.00 USD — a realized GAIN of 20.00 USD.
INVOICE_AMOUNT = Decimal("1000.00")
INVOICE_CURRENCY = "EUR"
ACCRUAL_RATE = Decimal("1.10")
SOURCE_AMOUNT = Decimal("1080.00")
EXPECTED_REALIZED_FX = "20.00"


# ---------------------------------------------------------------------------
# Reconciler-side fakes
# ---------------------------------------------------------------------------


def _fx_payment():
    return SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        provider_payment_id="px_fx",
        payment_run_id=uuid.uuid4(),
        status="submitted",
        submitted_at=None,
        completed_at=None,
        failure_reason=None,
        reference=None,
        method="international_wire",
        amount=INVOICE_AMOUNT,
        source_amount=SOURCE_AMOUNT,
        source_currency="USD",
        settled_amount=None,
        settled_currency=None,
        settled_amount_unstorable=False,
    )


def _fx_invoice(payment):
    return SimpleNamespace(
        id=payment.invoice_id,
        entity_id=uuid.uuid4(),
        currency=INVOICE_CURRENCY,
        amount=INVOICE_AMOUNT,
        reporting_currency="USD",
        reporting_fx_rate=ACCRUAL_RATE,
        invoice_number="INV-FX-1",
    )


def _reconciler_session(payment, invoice):
    """A tenant session that answers the sweep's two reads.

    Dispatches on the statement's target table rather than call order, so an
    extra query (the fraud-flag dedupe on a discrepancy) can't silently shift
    which fake row a read gets.
    """
    from tests.test_payment_settlement_webhook import _target_table

    def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        table = _target_table(stmt)
        if table == "invoices":
            result.scalar_one_or_none = MagicMock(return_value=invoice)
        elif table == "exceptions":
            result.scalar = MagicMock(return_value=0)
        else:
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=[payment])
            result.scalars = MagicMock(return_value=scalars)
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, db


async def _run_reconciler(payment, invoice, *, reported=INVOICE_AMOUNT, currency="EUR"):
    """Drive one backstop sweep to a `completed` resolution and return the
    `dispatch_audit` mock it wrote through."""
    from datetime import UTC, datetime, timedelta

    from app.services.payment_reconciler import _reconcile_tenant

    payment.submitted_at = datetime.now(UTC) - timedelta(hours=2)
    factory, _ = _reconciler_session(payment, invoice)
    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.get_payment_status = AsyncMock(return_value=PaymentStatus.completed)
    adapter.fetch_settlement = AsyncMock(
        return_value=SettlementReport(available=True, amount=reported, currency=currency)
    )
    audit = AsyncMock()
    with (
        patch(
            "app.services.payment_reconciler.create_async_engine",
            return_value=MagicMock(dispose=AsyncMock()),
        ),
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=adapter),
        patch("app.services.audit_dispatch.dispatch_audit", audit),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.services.exception_service.create_exception", AsyncMock()),
    ):
        outcome = await _reconcile_tenant(org, datetime.now(UTC))
    # A swallowed per-payment failure would leave the audit mock un-awaited and
    # make every assertion below fail for the wrong reason.
    assert outcome["payment_failures"] == 0, "the sweep swallowed a per-payment error"
    assert outcome["resolved"] == 1
    return audit


# ---------------------------------------------------------------------------
# The gap: realized FX on the backstop path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_completion_books_realized_fx_gain_loss():
    """The regression. Before the shared recorder, the reconciler wrote a
    `payment.completed` audit row carrying only `details.settlement` — the
    realized FX gain/loss on a cross-currency settlement was simply absent."""
    payment = _fx_payment()
    audit = await _run_reconciler(payment, _fx_invoice(payment))

    audit.assert_awaited_once()
    details = audit.call_args.kwargs["details"]
    assert details["source"] == "reconciler_poll"
    assert details["realized_fx_gain_loss"] == EXPECTED_REALIZED_FX
    # Money is an exact decimal string on the audit row, never a float.
    assert isinstance(details["realized_fx_gain_loss"], str)


@pytest.mark.asyncio
async def test_backstop_and_webhook_book_the_same_realized_fx_figure():
    """Parity, not merely presence: the same two rows must produce the same
    realized figure whichever path happened to settle the payment."""
    from tests import test_payment_settlement_webhook as h

    recon_payment = _fx_payment()
    recon_audit = await _run_reconciler(recon_payment, _fx_invoice(recon_payment))

    hook_payment = h._payment(
        amount=INVOICE_AMOUNT,
        source_amount=SOURCE_AMOUNT,
        source_currency="USD",
    )
    hook_invoice = h._invoice(
        currency=INVOICE_CURRENCY,
        amount=INVOICE_AMOUNT,
        reporting_fx_rate=ACCRUAL_RATE,
        reporting_currency="USD",
    )
    tenant_factory, _ = h._tenant_session_factory(hook_payment, hook_invoice)
    mocks = await h._run(
        h._org(),
        h._adapter(amount=INVOICE_AMOUNT, currency=INVOICE_CURRENCY),
        tenant_factory,
    )

    hook_details = mocks["audit"].call_args.kwargs["details"]
    recon_details = recon_audit.call_args.kwargs["details"]
    assert hook_details["realized_fx_gain_loss"] == recon_details["realized_fx_gain_loss"]
    assert recon_details["realized_fx_gain_loss"] == EXPECTED_REALIZED_FX
    # And the settlement verdict block stays identical in shape and outcome.
    assert hook_details["settlement"]["outcome"] == recon_details["settlement"]["outcome"]


@pytest.mark.asyncio
async def test_a_domestic_backstop_completion_books_no_fx_figure():
    """Absent, never zero — a zero would assert we measured an exposure that
    does not exist. Same posture the webhook path already had."""
    payment = _fx_payment()
    payment.source_amount = None
    payment.source_currency = None
    payment.method = "ach"
    invoice = _fx_invoice(payment)
    invoice.currency = "USD"
    invoice.reporting_fx_rate = None

    audit = await _run_reconciler(payment, invoice, reported=INVOICE_AMOUNT, currency="USD")

    details = audit.call_args.kwargs["details"]
    assert "realized_fx_gain_loss" not in details
    assert details["settlement"]["outcome"] == "matched"


@pytest.mark.asyncio
async def test_a_missing_invoice_row_still_completes_without_an_fx_figure():
    """A payment whose invoice row is gone has no accrual to measure against;
    the completion must still be recorded rather than raising on the money
    path."""
    payment = _fx_payment()
    audit = await _run_reconciler(payment, None)

    details = audit.call_args.kwargs["details"]
    assert details["status"] == "completed"
    assert "realized_fx_gain_loss" not in details


# ---------------------------------------------------------------------------
# Drift guard — one owner, both paths
# ---------------------------------------------------------------------------


def _sources_under_app() -> list[Path]:
    return sorted(APP_ROOT.rglob("*.py"))


def test_only_the_shared_recorder_computes_realized_fx_for_a_settlement():
    """The drift guard.

    `realized_fx_gain_loss_for_settlement` may be CALLED from exactly one
    place: `services/payment_settlement_record.record_completion`, which both
    completion paths go through. A third path that copies the block instead of
    calling the owner is how the reconciler lost the figure in the first
    place, so it fails here rather than shipping silently.
    """
    callers = {
        path.relative_to(APP_ROOT).as_posix()
        for path in _sources_under_app()
        # The lookbehind skips the definition site in
        # `services/international_payments.py`; every other paren-form
        # occurrence is a CALL.
        if re.search(r"(?<!def )realized_fx_gain_loss_for_settlement\(", path.read_text())
    }
    assert callers == {"services/payment_settlement_record.py"}, (
        "realized FX for a settlement must be computed only by "
        "payment_settlement_record.record_completion; found extra caller(s): "
        f"{sorted(callers - {'services/payment_settlement_record.py'})}"
    )


def test_both_completion_paths_go_through_the_shared_recorder():
    """Neither the webhook nor the backstop may call `record_settlement`
    directly: doing so books the settlement verdict while skipping the
    realized-FX half, which is exactly the shape of the bug."""
    webhook = (APP_ROOT / "api" / "payments.py").read_text()
    backstop = (APP_ROOT / "services" / "payment_reconciler.py").read_text()

    for name, source in (("api/payments.py", webhook), ("payment_reconciler.py", backstop)):
        assert re.search(r"^\s*completion = await record_completion\(", source, re.M), (
            f"{name} must reach a completion through record_completion"
        )
        assert not re.search(r"^\s*\w+ = await record_settlement\(", source, re.M), (
            f"{name} calls record_settlement directly — that skips realized FX"
        )
