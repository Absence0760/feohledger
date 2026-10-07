"""Notification templates — pure event -> (subject, in-app title/body, email).

Every template renders from a small, deliberately PII-free context: invoice
number, vendor name, amount, currency, and status. **Never** bank details, tax
IDs, full vendor addresses, or payment-method numbers — these strings land in
emails and log-adjacent in-app rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.models.notification import (
    EVENT_CHAT_MESSAGE,
    EVENT_INVOICE_APPROVED,
    EVENT_INVOICE_ASSIGNED,
    EVENT_INVOICE_PAID,
    EVENT_INVOICE_REJECTED,
)


@dataclass(frozen=True)
class InvoiceContext:
    """The minimal, PII-free invoice fields a template may reference."""

    invoice_number: str
    vendor_name: str
    amount: Decimal | None = None
    currency: str = "USD"
    reason: str | None = None  # e.g. rejection reason — free text, no PII expected
    # Short, PII-free chat snippet (e.g. an author label like "from supplier").
    # NEVER the raw message body — see EVENT_CHAT_MESSAGE render branch.
    note: str | None = None


@dataclass(frozen=True)
class RenderedNotification:
    title: str  # in-app title + email subject
    body_text: str  # in-app body + email plaintext
    body_html: str | None = None


def _money(ctx: InvoiceContext) -> str:
    if ctx.amount is None:
        return ""
    # Render exactly from the Decimal — never coerce currency through float.
    return f" for {ctx.currency} {ctx.amount:,.2f}"


# Map the canonical event_type strings to the catalogue key prefix. The
# catalogue (app/services/email_adapters/email_catalogue.py) owns the per-locale
# copy; this module stays the thin event → (title, body) renderer.
_EVENT_TO_KEY = {
    EVENT_INVOICE_ASSIGNED: "invoice_assigned",
    EVENT_INVOICE_APPROVED: "invoice_approved",
    EVENT_INVOICE_REJECTED: "invoice_rejected",
    EVENT_INVOICE_PAID: "invoice_paid",
    EVENT_CHAT_MESSAGE: "chat_message",
}


def render(
    event_type: str, ctx: InvoiceContext, *, locale: str | None = None
) -> RenderedNotification:
    """Render the notification for `event_type`. Raises on unknown events.

    ``locale`` selects the email-copy language (account-level DB ``locale``
    preference of the recipient). Defaults to ``None`` → English, which is what
    the in-app notification center always uses (the in-app row is NOT localized
    by the recipient's email locale — see ``docs/notifications.md``). The deep
    links, money amount, invoice number, and vendor name are interpolated as
    placeholders, so they stay identical across locales — only the copy changes.
    """
    # Imported lazily to keep this pure module free of the adapter package at
    # import time (avoids a cycle: adapters never import templates).
    from app.services.email_adapters.email_catalogue import translate

    key = _EVENT_TO_KEY.get(event_type)
    if key is None:
        raise ValueError(f"No notification template for event type '{event_type}'")

    ref = f"Invoice {ctx.invoice_number} ({ctx.vendor_name})"
    money = _money(ctx)

    title = translate(f"notif.{key}.title", locale, ref=ref)
    body = translate(f"notif.{key}.body", locale, ref=ref, money=money)

    if event_type == EVENT_INVOICE_REJECTED and ctx.reason:
        body += translate("notif.invoice_rejected.reason", locale, reason=ctx.reason)
    elif event_type == EVENT_CHAT_MESSAGE and ctx.note:
        # NEVER put the raw message body into title/body. ctx.note may carry at
        # most a short author label (e.g. "from supplier") — no message text.
        body += translate("notif.chat_message.note", locale, note=ctx.note)

    body_html = f"<p>{body}</p>"
    return RenderedNotification(title=title, body_text=body, body_html=body_html)


def render_contract_renewal(
    *,
    contract_number: str,
    vendor_name: str | None,
    end_date: date,
    days_until: int,
) -> RenderedNotification:
    """Render the contract-renewal-due notification (PII-free).

    Built separately from `render` because it carries a contract context, not
    an invoice one — the dispatcher passes the result through as a pre-rendered
    notification.
    """
    vendor = f" with {vendor_name}" if vendor_name else ""
    when = "today" if days_until <= 0 else f"in {days_until} day{'s' if days_until != 1 else ''}"
    title = f"Contract {contract_number} expires {when}"
    body = (
        f"Contract {contract_number}{vendor} expires on {end_date.isoformat()} ({when}). "
        "Review it for renewal."
    )
    return RenderedNotification(title=title, body_text=body, body_html=f"<p>{body}</p>")


def render_cash_shortfall(
    *,
    period: str,
    closing: Decimal,
    threshold: Decimal,
    shortfall: Decimal,
    currency: str,
    breach_count: int,
    unconverted_count: int = 0,
) -> RenderedNotification:
    """Render the projected-cash-shortfall notification (PII-free).

    Like `render_contract_renewal`, this carries its own context rather than an
    invoice one, so the dispatcher passes it through pre-rendered. Every figure
    formats straight off the `Decimal` — money never round-trips through float
    on its way into an email.

    The numbers are org-level aggregates (a projected closing balance, the
    configured minimum), never per-vendor or per-invoice detail, so nothing
    PII-bearing reaches the message.
    """
    more = (
        ""
        if breach_count <= 1
        else f" {breach_count} periods in the forecast close below the minimum."
    )
    # A projected shortfall built partly on figures we could not convert into
    # the reporting currency is not a number to act on without knowing that.
    # One unconverted ¥10,000,000 invoice on a USD curve manufactures a
    # shortfall outright, and this email is what sends finance leaders after
    # it. Count only — PII-free, like every other figure here.
    caveat = (
        ""
        if unconverted_count <= 0
        else (
            f" Note: {unconverted_count} commitment(s) could not be converted to "
            f"{currency} and were included at face value — confirm before acting."
        )
    )
    title = f"Projected cash shortfall in {period}"
    body = (
        f"Your projected cash balance closes at {currency} {closing:,.2f} in {period} — "
        f"{currency} {shortfall:,.2f} below your {currency} {threshold:,.2f} minimum."
        f"{more}{caveat} Review the cash-flow forecast before scheduling more payments."
    )
    return RenderedNotification(title=title, body_text=body, body_html=f"<p>{body}</p>")


#: The AI-read-invoice usage notices (decisions §253), mildest first.
AI_NOTICE_80 = "80"
AI_NOTICE_100 = "100"
AI_NOTICE_CAP = "cap"


def render_ai_invoice_usage(
    *,
    notice: str,
    period: str,
    used: int,
    included: int,
    overage_unit_price: Decimal | None,
    spend_cap: Decimal | None,
    currency: str = "USD",
) -> RenderedNotification:
    """Render an AI-read-invoice usage notice (80% / 100% / spending cap).

    Org-level counts and the plan's own prices only — no invoice, vendor or
    person is named, so nothing PII-bearing reaches the email. Money formats
    straight off the ``Decimal``. ``overage_unit_price is None`` is the Free
    tier, where the limit pauses AI reading instead of billing.
    """
    pauses = overage_unit_price is None
    if notice == AI_NOTICE_CAP:
        cap_text = f"{currency} {spend_cap:,.2f}" if spend_cap is not None else "your cap"
        title = f"AI spending cap reached for {period}"
        body = (
            f"This month's AI-read invoice overage has reached your spending cap of {cap_text}. "
            "AI reading is paused until next month: new invoices are still created and wait "
            "for manual entry, and approvals and payments are unaffected. Raise the cap on "
            "the Billing page to resume AI reading now."
        )
    elif notice == AI_NOTICE_100:
        title = f"All {included:,} AI-read invoices for {period} are used"
        if pauses:
            tail = (
                "AI reading is now paused until next month: new invoices are still created "
                "and wait for manual entry, and approvals and payments are unaffected. "
                "Upgrade your plan on the Billing page to keep AI reading on."
            )
        else:
            tail = (
                f"Each further AI-read invoice this month is billed at {currency} "
                f"{overage_unit_price}. Set or review a monthly spending cap on the Billing page."
            )
        body = (
            f"Your organization has used all {included:,} AI-read invoices included "
            f"this month. {tail}"
        )
    else:
        title = f"80% of this month's AI-read invoices are used ({period})"
        if pauses:
            tail = (
                "When the allowance is used, AI reading pauses until next month and new "
                "invoices wait for manual entry. Upgrade your plan on the Billing page to avoid it."
            )
        else:
            tail = (
                f"Past the allowance, each AI-read invoice is billed at {currency} "
                f"{overage_unit_price}."
            )
        body = (
            f"Your organization has used {used:,} of the {included:,} AI-read invoices "
            f"included this month. {tail}"
        )
    return RenderedNotification(title=title, body_text=body, body_html=f"<p>{body}</p>")
