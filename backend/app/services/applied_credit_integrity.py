"""An edited invoice must still satisfy every credit memo already applied to it.

`api/credit_memos.py` refuses to APPLY a memo unless the invoice's vendor and
currency match the memo's and its remaining balance absorbs the amount. Those
checks run once, at application time — and an applied memo is immutable (it can
be neither voided nor re-applied). But the invoice it landed on stays editable
until approval: `PATCH /api/invoices/{id}` and approve-with-corrections
(`services/review.approve_invoice`) can both re-save the vendor (which re-links
`Invoice.vendor_id`), the currency, or the amount afterwards. Each of those
silently undid a property the apply had established, and
`services/payment_runs.net_payable_amount` then netted the memo off whatever the
invoice had become:

- **vendor** — vendor A's credit reduced what vendor B is paid;
- **currency** — a USD credit was subtracted, digit for digit, from a EUR payable;
- **amount** — lowering the amount below the credits already applied consumed
  the excess against nothing (the net went negative, the payment was refused as
  "fully credited", and the stranded part of the credit was lost for good).

So every path that changes those fields on an invoice calls
:func:`refuse_edit_stranding_applied_credits` AFTER applying the change (and
after any vendor re-link), inside the same transaction: a refusal raises, and
the caller's transaction rolls back with the invoice untouched. The rule is
the apply guards' rule, read in the other direction — the same NULL-vendor
fail-closed, the same case-insensitive currency comparison that admits a blank
invoice currency — so a pairing apply would refuse cannot be reached by editing
the invoice instead. `docs/decisions.md` §214.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_memo import CreditMemo
from app.models.invoice import Invoice

_VENDOR_DETAIL = (
    "This invoice carries applied credit memos from its current vendor; it cannot be "
    "re-pointed at another vendor (or left without one), or that vendor's credit would "
    "reduce a different supplier's payment"
)
_CURRENCY_DETAIL = (
    "This invoice carries applied credit memos in {memo_currency}; its currency cannot "
    "change to {invoice_currency}, or the credit would be netted across currencies"
)
_AMOUNT_DETAIL = (
    "The invoice amount cannot be lowered below the {applied} already credited to it by "
    "applied credit memos"
)


def _norm_currency(value: str | None) -> str:
    return (value or "").strip().upper()


async def refuse_edit_stranding_applied_credits(
    db: AsyncSession, invoice: Invoice, *, currency_edited: bool = False
) -> None:
    """409 if ``invoice``, as it now stands, breaks a credit applied to it.

    Call after the edit has been written onto the ORM object (the query below
    autoflushes it, inside the caller's transaction). A no-op for an invoice
    with no applied memo, which is almost every invoice.

    ``currency_edited`` marks an edit that wrote the currency itself. A blank
    invoice currency is admitted only when it was already blank — the legacy
    rows the apply guard admits — never when this edit is what blanked it,
    which would just be a way to strip the currency out from under a credit.
    """
    memos = (
        await db.execute(
            select(CreditMemo.vendor_id, CreditMemo.currency, CreditMemo.amount).where(
                CreditMemo.invoice_id == invoice.id,
                CreditMemo.status == "applied",
            )
        )
    ).all()
    if not memos:
        return

    if invoice.vendor_id is None or any(m.vendor_id != invoice.vendor_id for m in memos):
        raise HTTPException(status_code=409, detail=_VENDOR_DETAIL)

    invoice_currency = _norm_currency(invoice.currency)
    if invoice_currency or currency_edited:
        for memo in memos:
            memo_currency = _norm_currency(memo.currency)
            if memo_currency and memo_currency != invoice_currency:
                raise HTTPException(
                    status_code=409,
                    detail=_CURRENCY_DETAIL.format(
                        memo_currency=memo_currency,
                        invoice_currency=invoice_currency or "no currency",
                    ),
                )

    applied = sum((m.amount for m in memos), Decimal("0"))
    if (invoice.amount or Decimal("0")) < applied:
        raise HTTPException(status_code=409, detail=_AMOUNT_DETAIL.format(applied=applied))
