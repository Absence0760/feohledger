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

So every request-driven path that changes those fields calls
:func:`refuse_edit_stranding_applied_credits` AFTER applying the change (and
after any vendor re-link), inside the same transaction: a refusal raises, and
the caller's transaction rolls back with the invoice untouched. The rule is
the apply guards' rule, read in the other direction — the same NULL-vendor
fail-closed, the same case-insensitive currency comparison that admits a blank
invoice currency — so a pairing apply would refuse cannot be reached by editing
the invoice instead.

Re-extraction writes the same fields in the background, with no request to
refuse, so the pairing is ALSO re-checked at the point of payment
(:func:`applied_credit_conflicts` and its SQL twin
:func:`applied_credit_conflict_exists`): the run builder and the payment queue,
`POST /api/payments`, `/retry-failed`, and dispatch all refuse such an invoice
as `applied_credit_mismatch`. `docs/decisions.md` §214.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select
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


#: The two pairings a payment re-checks (`applied_credit_conflicts`). The amount
#: needs no third code there: credits above the amount make the net zero or
#: negative, which every payment path already refuses as `fully_credited`.
CONFLICT_VENDOR = "vendor"
CONFLICT_CURRENCY = "currency"

#: What Python's `str.strip()` removes; `btrim` with no list strips spaces only.
_STRIPPED_WHITESPACE = " \t\n\r\x0b\x0c"


def _norm_currency(value: str | None) -> str:
    return (value or "").strip().upper()


def _pairing_conflict(
    invoice_vendor_id, invoice_currency: str | None, memos, *, blank_is_a_change: bool
) -> tuple[str, str] | None:
    """``(kind, memo_currency)`` for the first broken pairing, else ``None``.

    The one statement of the rule, shared by the edit guard and the payment
    re-check: the invoice's vendor must provably equal every memo's (a NULL
    link fails closed), and its currency must match every memo that names one,
    case- and space-insensitively. A blank invoice currency is admitted — the
    legacy rows the apply guard admits — unless ``blank_is_a_change``.
    """
    if not memos:
        return None
    if invoice_vendor_id is None or any(m.vendor_id != invoice_vendor_id for m in memos):
        return CONFLICT_VENDOR, ""
    current = _norm_currency(invoice_currency)
    if current or blank_is_a_change:
        for memo in memos:
            memo_currency = _norm_currency(memo.currency)
            if memo_currency and memo_currency != current:
                return CONFLICT_CURRENCY, memo_currency
    return None


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

    conflict = _pairing_conflict(
        invoice.vendor_id, invoice.currency, memos, blank_is_a_change=currency_edited
    )
    if conflict is not None:
        kind, memo_currency = conflict
        if kind == CONFLICT_VENDOR:
            raise HTTPException(status_code=409, detail=_VENDOR_DETAIL)
        raise HTTPException(
            status_code=409,
            detail=_CURRENCY_DETAIL.format(
                memo_currency=memo_currency,
                invoice_currency=_norm_currency(invoice.currency) or "no currency",
            ),
        )

    applied = sum((m.amount for m in memos), Decimal("0"))
    if (invoice.amount or Decimal("0")) < applied:
        raise HTTPException(status_code=409, detail=_AMOUNT_DETAIL.format(applied=applied))


async def applied_credit_conflicts(
    db: AsyncSession, invoices: Iterable[Invoice]
) -> dict[uuid.UUID, str]:
    """``{invoice id: CONFLICT_*}`` for invoices whose applied credits no longer
    pair with them — the point-of-payment backstop.

    The edit guard above covers every request-driven writer, but re-extraction
    (a manual re-extract, the supplier portal's resubmit) writes amount,
    currency and — on a manual re-extract — the vendor link in the background,
    where there is no request to refuse. So every path that books or dispatches
    money re-checks the pairing here: the run builder and the queue (through
    `payment_runs.run_refusal_reasons`), `POST /api/payments`, and dispatch
    (`api/payments._execute_single_payment`). One grouped query for the batch;
    invoices with no applied memo are absent.
    """
    rows = list(invoices)
    if not rows:
        return {}
    memos_by_invoice: dict[uuid.UUID, list] = {}
    for memo in (
        await db.execute(
            select(CreditMemo.invoice_id, CreditMemo.vendor_id, CreditMemo.currency).where(
                CreditMemo.invoice_id.in_([inv.id for inv in rows]),
                CreditMemo.status == "applied",
            )
        )
    ).all():
        memos_by_invoice.setdefault(memo.invoice_id, []).append(memo)
    out: dict[uuid.UUID, str] = {}
    for inv in rows:
        conflict = _pairing_conflict(
            inv.vendor_id, inv.currency, memos_by_invoice.get(inv.id), blank_is_a_change=False
        )
        if conflict is not None:
            out[inv.id] = conflict[0]
    return out


def applied_credit_conflict_exists():
    """SQL form of :func:`applied_credit_conflicts`, correlated to ``Invoice``.

    For the payment queue's whole-set aggregates, which must not stream every
    invoice into Python; `tests/test_payment_queue_blocked.py` holds it to the
    Python verdict.
    """

    def _sql_norm(column):
        return func.upper(func.btrim(func.coalesce(column, ""), _STRIPPED_WHITESPACE))

    invoice_currency = _sql_norm(Invoice.currency)
    memo_currency = _sql_norm(CreditMemo.currency)
    return exists(
        select(1).where(
            CreditMemo.invoice_id == Invoice.id,
            CreditMemo.status == "applied",
            or_(
                Invoice.vendor_id.is_(None),
                CreditMemo.vendor_id != Invoice.vendor_id,
                and_(
                    memo_currency != "", invoice_currency != "", memo_currency != invoice_currency
                ),
            ),
        )
    )
