"""Tests for the PO matching → invoice_warnings → exception queue wiring.

The matching algorithm itself is pure-Python and gets covered indirectly here.
What we want to lock down is the *integration*: the post-extraction hook now
runs PO matching, persists the structured result on `invoice.po_match`, and
routes mismatches into the exception queue. Skipping any of those steps
silently regresses the "PO-gated invoice" workflow.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# ---------- MatchResult shape contract -------------------------------------


def test_match_result_serialises_to_jsonb_friendly_dict():
    """`po_match` is a JSONB column. Every field must round-trip through
    `asdict` cleanly — no enums, no datetimes, no Decimals."""
    from app.services.po_matching import MatchResult

    m = MatchResult(
        match_type="2-way",
        status="matched",
        po_id="po-uuid",
        po_number="PO-001",
        po_total=1000.0,
        amount_variance=10.0,
        amount_variance_pct=1.0,
        within_tolerance=True,
        issues=["minor variance"],
        details={"k": "v"},
    )
    d = asdict(m)
    # Sanity-check the keys the frontend reads
    for key in (
        "status",
        "match_type",
        "po_id",
        "po_number",
        "po_total",
        "amount_variance",
        "amount_variance_pct",
        "within_tolerance",
        "issues",
        "details",
    ):
        assert key in d


# ---------- _refresh_po_match integration ---------------------------------


def _fake_invoice(
    *, po_number="PO-001", amount=100.0, status_value="ready_for_review", currency="USD"
):
    """Minimal Invoice stand-in — only the attrs the PO-match code touches."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        po_number=po_number,
        amount=amount,
        # The warning sentences are money-bearing, so they name the invoice's
        # own currency rather than a hardcoded `$` (decisions.md §157).
        currency=currency,
        po_match=None,
        contract_id=None,
        # _refresh_po_match resolves the per-vendor/commodity match rule, which
        # reads these — None falls through to the org/hardcoded default.
        vendor_id=None,
        gl_account=None,
        status=SimpleNamespace(value=status_value),
    )


@pytest.mark.asyncio
async def test_refresh_po_match_persists_matched_result_without_exception():
    """A clean match writes po_match but doesn't add a warning or exception."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="2-way",
        status="matched",
        po_id="x",
        po_number="PO-001",
        po_total=100.0,
        amount_variance=0.0,
        amount_variance_pct=0.0,
        within_tolerance=True,
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert warnings == []
    ensure.assert_not_awaited()
    assert inv.po_match["status"] == "matched"
    assert inv.po_match["po_number"] == "PO-001"


@pytest.mark.asyncio
async def test_refresh_po_match_creates_exception_on_amount_mismatch():
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice(amount=120.0)
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="2-way",
        status="mismatch",
        po_id="x",
        po_number="PO-001",
        po_total=100.0,
        amount_variance=20.0,
        amount_variance_pct=20.0,
        within_tolerance=False,
        issues=["Amount mismatch: invoice 120.00 USD vs PO 100.00 USD (+20.0%)"],
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert len(warnings) == 1
    assert warnings[0]["type"] == "po_mismatch"
    assert warnings[0]["severity"] == "warning"
    assert "20.0%" in warnings[0]["message"]
    ensure.assert_awaited_once()
    # Exception type must match the registered EXCEPTION_TYPE_LABELS key.
    assert ensure.await_args.args[2] == "po_mismatch"


@pytest.mark.asyncio
async def test_refresh_po_match_currency_mismatch_names_both_codes_and_no_variance():
    """Invoice and PO in different currencies: the finding is the codes, not a
    variance — `po_amount_variance` would print a percentage between EUR and USD
    figures. Same severity and exception type as an amount mismatch
    (decisions §197)."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice(amount=Decimal("100.00"), currency="eur")
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="2-way",
        status="mismatch",
        po_number="PO-001",
        po_total=Decimal("100.00"),
        po_currency="USD",
        currency_check="different",
        amount_variance=None,
        amount_variance_pct=None,
        within_tolerance=False,
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert [w["code"] for w in warnings] == ["po_currency_mismatch"]
    assert warnings[0]["params"] == {
        "invoiceCurrency": "EUR",
        "poNumber": "PO-001",
        "poCurrency": "USD",
    }
    assert warnings[0]["severity"] == "warning"
    ensure.assert_awaited_once()
    assert ensure.await_args.args[2:4] == ("po_mismatch", "warning")
    assert inv.po_match["currency_check"] == "different"
    assert inv.po_match["amount_variance"] is None


@pytest.mark.asyncio
async def test_refresh_po_match_unknown_po_currency_leaves_the_po_figure_unlabelled():
    """A PO with no currency is compared at face value; its figure must not
    borrow the invoice's code, so it rides a `number` param, not `money`."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice(amount=Decimal("120.00"), currency="GBP")
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="2-way",
        status="mismatch",
        po_number="PO-001",
        po_total=Decimal("100.00"),
        po_currency=None,
        currency_check="unknown",
        amount_variance=Decimal("20.00"),
        amount_variance_pct=Decimal("20.0"),
        within_tolerance=False,
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()),
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert [w["code"] for w in warnings] == ["po_amount_variance_po_currency_unknown"]
    assert warnings[0]["params"]["poTotal"] == "100.00"
    assert warnings[0]["message"] == (
        "Amount variance +20.0% vs PO PO-001, which records no currency "
        "(invoice 120.00 GBP vs PO 100.00)"
    )


@pytest.mark.asyncio
async def test_refresh_po_match_creates_error_when_po_not_found():
    """A reference to a non-existent PO is the loudest signal — error severity."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(status="no_po", issues=["PO PO-001 not found"])

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert warnings[0]["severity"] == "error"
    assert ensure.await_args.kwargs == {} or ensure.await_args.args[3] == "error"


@pytest.mark.asyncio
async def test_refresh_po_match_partial_is_info_severity():
    """A partial 3-way match (goods in transit) is informational, not an error."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="3-way",
        status="partial",
        po_number="PO-001",
        po_total=100.0,
        within_tolerance=True,
        # What the matcher sets alongside a short receipt.
        ordered_quantity=Decimal("10"),
        received_quantity=Decimal("6"),
        issues=["Partial receipt: 60% of ordered quantity received"],
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()),
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert warnings[0]["severity"] == "info"


@pytest.mark.asyncio
async def test_a_failed_inspection_raises_no_amount_warning_on_an_in_tolerance_invoice():
    """`status` is shared by the amount leg and the 4-way leg. A failed
    inspection sets `mismatch` on an invoice whose amount matched, and keyed on
    `status` alone that raised "Amount variance +0.0%" plus a po_mismatch
    exception beside the quality hold that was the real finding."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="4-way",
        status="mismatch",
        po_number="PO-001",
        po_total=Decimal("100.00"),
        po_currency="USD",
        currency_check="same",
        amount_variance=Decimal("0"),
        amount_variance_pct=Decimal("0"),
        within_tolerance=True,
        inspection_result="fail",
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert [w["code"] for w in warnings] == ["quality_inspection_failed"]
    assert [call.args[2] for call in ensure.await_args_list] == ["quality_hold"]


@pytest.mark.asyncio
async def test_a_partial_acceptance_on_a_full_receipt_is_not_a_partial_receipt():
    """A partial quality acceptance sets `partial` on goods that ALL arrived;
    the receipt sentence ("only part of the ordered quantity has been
    received") would be false there."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="4-way",
        status="partial",
        po_number="PO-001",
        po_total=Decimal("100.00"),
        within_tolerance=True,
        ordered_quantity=Decimal("10"),
        received_quantity=Decimal("10"),
        inspection_result="partial",
        inspection_accepted_quantity=7.0,
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert [w["code"] for w in warnings] == ["quality_partial_acceptance"]
    assert [call.args[2] for call in ensure.await_args_list] == ["quality_hold"]


# ---------- over-receipt reaches the exception queue -----------------------


@pytest.mark.asyncio
async def test_refresh_po_match_raises_on_over_receipt():
    """An over-receipt must reach the exception queue, not just the modal.

    The matcher flags `received > ordered` on `po_match.over_receipt` and
    renders it into the invoice modal, but nothing here raised it — so no
    exception row was ever opened and no clerk was asked about quantities
    nobody ordered. `warning`, not the `info` a partial receipt gets: a short
    delivery is routinely benign (goods in transit), whereas an over-delivery
    cannot be explained by timing, and it is how an invoice for unauthorised
    quantities acquires its supporting receipt.

    It is raised INDEPENDENTLY of `status` — the amount control owns `status`,
    so an over-receipt rides alongside a perfectly `matched` invoice, which is
    exactly the case that used to disappear.
    """
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="3-way",
        status="matched",
        po_number="PO-001",
        po_total=100.0,
        within_tolerance=True,
        over_receipt=True,
        ordered_quantity=Decimal("10"),
        received_quantity=Decimal("14"),
        issues=["Over-receipt: 14 received against 10 ordered (+4)"],
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert len(warnings) == 1, warnings
    assert warnings[0]["type"] == "po_mismatch"
    assert warnings[0]["severity"] == "warning"
    # The matcher's own composed detail is reused verbatim, plus the PO ref.
    assert "Over-receipt: 14 received against 10 ordered (+4)" in warnings[0]["message"]
    assert "PO-001" in warnings[0]["message"]

    ensure.assert_awaited_once()
    assert ensure.await_args.args[2] == "po_mismatch"
    assert ensure.await_args.args[3] == "warning"


@pytest.mark.asyncio
async def test_refresh_po_match_over_receipt_rides_alongside_an_amount_mismatch():
    """Both legs report. The amount message stays the amount message.

    `_ensure_exception` de-dupes per (invoice, type, open), so the second call
    is a no-op at the DB — but the WARNING must still land, because that is
    where the reviewer reads why the invoice is flagged, and the amount
    branch's message says nothing about quantities.
    """
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="3-way",
        status="mismatch",
        po_number="PO-001",
        po_total=100.0,
        amount_variance=50.0,
        amount_variance_pct=50.0,
        within_tolerance=False,
        over_receipt=True,
        ordered_quantity=Decimal("10"),
        received_quantity=Decimal("14"),
        issues=[
            "Amount mismatch: invoice 150.00 USD vs PO 100.00 USD (+50.0%)",
            "Over-receipt: 14 received against 10 ordered (+4)",
        ],
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()),
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    messages = [w["message"] for w in warnings]
    assert len(messages) == 2, messages
    assert any("Amount variance" in m for m in messages)
    assert any("Over-receipt" in m for m in messages)
    # The amount-variance message is untouched — `mismatch` stays the amount
    # control's, and its text must not start describing quantities.
    amount_msg = next(m for m in messages if "Amount variance" in m)
    assert "Over-receipt" not in amount_msg


@pytest.mark.asyncio
async def test_refresh_po_match_no_over_receipt_raises_nothing_extra():
    """The default `over_receipt=False` adds no warning — narrow by design."""
    from app.services import invoice_warnings
    from app.services.po_matching import MatchResult

    inv = _fake_invoice()
    warnings: list[dict] = []
    fake_match = MatchResult(
        match_type="3-way",
        status="matched",
        po_number="PO-001",
        po_total=100.0,
        within_tolerance=True,
    )

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock(return_value=fake_match)),
        patch.object(invoice_warnings, "_ensure_exception", AsyncMock()) as ensure,
    ):
        await invoice_warnings._refresh_po_match(db=AsyncMock(), invoice=inv, warnings=warnings)

    assert warnings == []
    ensure.assert_not_awaited()


# ---------- refresh_warnings skips PO matching when there's no po_number ----


@pytest.mark.asyncio
async def test_refresh_warnings_clears_po_match_when_po_number_removed():
    """If a reviewer removes the po_number from an invoice, the stale po_match
    must be cleared — otherwise the modal keeps showing the old result."""
    from app.services import invoice_warnings

    inv = _fake_invoice(po_number=None)
    inv.po_match = {"status": "matched", "po_number": "PO-OLD"}  # stale
    inv.vendor_name = "Acme"
    inv.invoice_number = "INV-1"
    inv.amount = 100.0
    inv.invoice_date = None
    inv.due_date = None
    inv.vendor_id = None
    inv.warnings = None

    with (
        patch.object(invoice_warnings, "match_invoice_to_po", AsyncMock()) as match,
        patch("sqlalchemy.ext.asyncio.AsyncSession.execute"),
    ):
        # Stub out the db calls refresh_warnings makes: the duplicate check
        # (`.scalar()`), its normalized-number fallback (`.all()` — the pass that
        # catches `INV-001` vs `INV-1`, reached only when the exact check misses,
        # which it does here), and the line-total reconciliation sum
        # (`.scalar_one_or_none()` — None means "no line totals to reconcile").
        # Each is a SYNC method on a real `Result`, so it must be a plain lambda:
        # left as an AsyncMock child, `.all()` returns a coroutine.
        db = AsyncMock()
        db.execute.return_value.scalar = lambda: 0
        db.execute.return_value.all = lambda: []
        db.execute.return_value.scalar_one_or_none = lambda: None
        await invoice_warnings.refresh_warnings(db, inv)

    match.assert_not_awaited()
    assert inv.po_match is None


# ---------- API contract --------------------------------------------------


def test_invoice_response_includes_po_match():
    """Frontend depends on this field to render the PO Match panel."""
    from app.schemas.invoice import InvoiceResponse

    fields = InvoiceResponse.model_fields
    assert "po_match" in fields
    # Must be optional / nullable — invoices without a PO have no match.
    assert fields["po_match"].default is None


# ---------- _status_str regression ---------------------------------------


def test_status_str_handles_plain_string():
    """Regression: PATCH /api/invoices/{id} sets `invoice.status` to a
    plain string (after Pydantic→.value conversion + setattr). The
    next call to refresh_warnings used to AttributeError on
    `invoice.status.value`. The helper has to accept all three shapes
    that reach refresh_warnings in practice."""
    from app.services.invoice_warnings import _status_str

    assert _status_str("ready_for_review") == "ready_for_review"


def test_status_str_handles_strenum():
    from app.models.invoice import InvoiceStatus
    from app.services.invoice_warnings import _status_str

    assert _status_str(InvoiceStatus.ready_for_review) == "ready_for_review"


def test_status_str_handles_simplenamespace_mock():
    """Existing pytest fixtures use SimpleNamespace(value=...) as a
    cheap stand-in for the StrEnum. Don't break those — many tests
    rely on the shape."""
    from types import SimpleNamespace

    from app.services.invoice_warnings import _status_str

    assert _status_str(SimpleNamespace(value="approved")) == "approved"
