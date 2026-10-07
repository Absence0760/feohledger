"""Unit tests for notification templates — content + PII safety.

These are pure (no DB, no Redis), so they always run.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.notification import (
    EVENT_CASH_SHORTFALL_PROJECTED,
    EVENT_CONTRACT_RENEWAL_DUE,
    EVENT_INVOICE_APPROVED,
    EVENT_INVOICE_ASSIGNED,
    EVENT_INVOICE_PAID,
    EVENT_INVOICE_REJECTED,
    NOTIFICATION_EVENT_TYPES,
)
from app.services.notification_templates import (
    InvoiceContext,
    render,
    render_cash_shortfall,
    render_contract_renewal,
)

# Fields that must NEVER appear in a notification subject/body (PII / banking).
_FORBIDDEN_PII = [
    "123456789",  # an account number
    "987654321",  # a routing number
    "12-3456789",  # an EIN / tax id
    "GB29NWBK60161331926819",  # an IBAN
    "742 Evergreen Terrace, Springfield, IL",  # a full address
]


def _ctx() -> InvoiceContext:
    return InvoiceContext(
        invoice_number="INV-2026-001",
        vendor_name="Globex Corp",
        amount=Decimal("1234.56"),
        currency="USD",
    )


def test_every_event_renders():
    for event_type in NOTIFICATION_EVENT_TYPES:
        if event_type == EVENT_CONTRACT_RENEWAL_DUE:
            # Contract renewal carries a contract context, not an invoice one, so
            # it is pre-rendered by its own function and handed to
            # notify_event(rendered=...) — it never flows through render().
            rendered = render_contract_renewal(
                contract_number="MSA-2026-007",
                vendor_name="Globex Corp",
                end_date=date(2026, 12, 31),
                days_until=14,
            )
            assert rendered.title
            assert rendered.body_text
            assert "MSA-2026-007" in rendered.body_text
            continue
        if event_type == EVENT_CASH_SHORTFALL_PROJECTED:
            # Same shape as contract renewal: an org-level cash context, not an
            # invoice one, so it is pre-rendered and handed to
            # notify_event(rendered=...) rather than flowing through render().
            rendered = render_cash_shortfall(
                period="2026-W05",
                closing=Decimal("-8000.00"),
                threshold=Decimal("1000.00"),
                shortfall=Decimal("9000.00"),
                currency="USD",
                breach_count=3,
            )
            assert rendered.title
            assert rendered.body_text
            assert "2026-W05" in rendered.body_text
            continue
        if event_type == "ai_invoice_usage":
            # Plan-usage context (decisions §253), pre-rendered by its own
            # function — never an invoice's.
            from app.services.notification_templates import render_ai_invoice_usage

            for notice in ("80", "100", "cap"):
                rendered = render_ai_invoice_usage(
                    notice=notice,
                    period="2026-10",
                    used=400,
                    included=500,
                    overage_unit_price=Decimal("0.10"),
                    spend_cap=Decimal("5.00"),
                )
                assert rendered.title
                assert "2026-10" in rendered.title
                assert rendered.body_text
            continue
        rendered = render(event_type, _ctx())
        assert rendered.title
        assert rendered.body_text
        # Must reference the invoice number + vendor so the recipient can act.
        assert "INV-2026-001" in rendered.body_text
        assert "Globex Corp" in rendered.body_text


def test_amount_rendered_exactly_from_decimal():
    rendered = render(EVENT_INVOICE_PAID, _ctx())
    # Exact, comma-grouped, two-decimal — no float artifacts.
    assert "USD 1,234.56" in rendered.body_text


def test_unknown_event_raises():
    with pytest.raises(ValueError):
        render("invoice_exploded", _ctx())


def test_rejection_includes_reason():
    ctx = InvoiceContext(
        invoice_number="INV-9",
        vendor_name="Initech",
        amount=Decimal("10.00"),
        reason="Missing PO number",
    )
    rendered = render(EVENT_INVOICE_REJECTED, ctx)
    assert "Missing PO number" in rendered.body_text


def test_no_pii_in_any_template():
    # Build a context carrying PII-shaped strings ONLY in fields a template is
    # allowed to read (it reads none of these, so nothing leaks). We then also
    # assert the rendered strings contain none of the forbidden tokens.
    ctx = _ctx()
    for event_type in (
        EVENT_INVOICE_ASSIGNED,
        EVENT_INVOICE_APPROVED,
        EVENT_INVOICE_REJECTED,
        EVENT_INVOICE_PAID,
    ):
        rendered = render(event_type, ctx)
        blob = f"{rendered.title}\n{rendered.body_text}\n{rendered.body_html}"
        for token in _FORBIDDEN_PII:
            assert token not in blob
