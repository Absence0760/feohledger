"""Split an :class:`InvoicePayload` into net / tax / gross bill lines, exactly.

Xero and Sage Business Cloud Accounting both derive a bill's total from its
lines. Our invariant is the opposite: ``Invoice.amount`` is what a payment run
pays and is **never recomputed from line items**
(``backend/docs/line-total-reconciliation.md``). So before an adapter posts, the
lines it sends must provably add up to ``payload.amount``, or the adapter
refuses rather than book a different total into the customer's ledger.

Line ``total`` semantics are not uniform across our ingest paths (the vision
adapter writes tax-inclusive totals, the e-invoice mapper tax-exclusive ones),
so the split is decided per invoice from the numbers themselves:

* ``tax_amount`` is zero/absent → every line is gross = net, no tax.
* the line amounts sum to ``amount`` → the lines are **tax-inclusive**.
* the line amounts plus ``tax_amount`` sum to ``amount`` → **tax-exclusive**.
* anything else (shipping or a discount carried only on the header, say) →
  ``amount_mismatch``.

Per-line tax is taken only from the invoice itself: each line's own ``tax``
when every line carries one and they sum to ``tax_amount``, or the whole
``tax_amount`` when there is a single line. Several lines with header-only tax
are never pro-rated (that would invent a split the supplier never stated):
tax-inclusive lines come back with ``tax=None`` (``inclusive_unsplit``) for an
ERP that can derive the split from its own tax rate while keeping the gross
exact (Xero); tax-exclusive lines are refused with ``tax_not_itemised``.

Not to be confused with ``bill_lines``, which picks the GL lines for the
adapters that take tax separately (Sage Intacct, SYSPRO).

Pure — no I/O — so both adapters and their tests share one statement of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.erp_adapters.base import ACCOUNT_NOT_LINKED, InvoicePayload

# Stable, PII-free refusal reasons beyond base.VENDOR_NOT_LINKED /
# ACCOUNT_NOT_LINKED. A refusal never reaches the ERP.
AMOUNT_MISMATCH = "amount_mismatch"
TAX_NOT_ITEMISED = "tax_not_itemised"
TAX_RATE_UNRESOLVED = "tax_rate_unresolved"
LINE_AMOUNT_MISSING = "line_amount_missing"
MISSING_DATES = "missing_dates"
DUPLICATE_DOCUMENT_NUMBER = "duplicate_document_number"


class BillRefusal(Exception):
    """The payload cannot be posted without inventing or dropping money."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class BillLine:
    description: str
    account_erp_id: str
    #: Tax-inclusive line amount. Always set; the lines' gross sums to ``amount``.
    gross: Decimal
    #: Tax on this line, or None when ``inclusive_unsplit``.
    tax: Decimal | None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None

    @property
    def net(self) -> Decimal | None:
        return None if self.tax is None else self.gross - self.tax


@dataclass(frozen=True)
class BillAllocation:
    lines: list[BillLine]
    tax_total: Decimal
    #: Lines are tax-inclusive but tax is only stated on the header.
    inclusive_unsplit: bool = False

    @property
    def has_tax(self) -> bool:
        return self.tax_total != 0


def _line_amount(li) -> Decimal | None:
    if li.total is not None:
        return li.total
    if li.quantity is not None and li.unit_price is not None:
        return li.quantity * li.unit_price
    return None


def allocate_bill_lines(payload: InvoicePayload) -> BillAllocation:
    """Return lines whose gross sums to ``payload.amount`` exactly, or raise."""
    tax_total = payload.tax_amount or Decimal(0)

    raw: list[tuple[str, str | None, Decimal, Decimal | None, object]] = []
    if payload.line_items:
        for li in payload.line_items:
            amount = _line_amount(li)
            if amount is None:
                raise BillRefusal(LINE_AMOUNT_MISSING)
            # A line posts on its own account's ERP id. Only an uncoded line
            # takes the header's account; a coded line with no id stays None
            # and is refused below, never moved onto the header's account.
            account = li.gl_account_erp_id or (None if li.gl_account else payload.gl_account_erp_id)
            description = li.description or payload.description or payload.invoice_number
            raw.append((description, account, amount, li.tax, li))
    else:
        description = payload.description or payload.invoice_number
        raw.append((description, payload.gl_account_erp_id, payload.amount, None, None))

    if any(account is None for _, account, _, _, _ in raw):
        raise BillRefusal(ACCOUNT_NOT_LINKED)

    amounts_sum = sum((a for _, _, a, _, _ in raw), Decimal(0))
    line_taxes = [t for _, _, _, t, _ in raw]
    itemised = all(t is not None for t in line_taxes) and (
        sum((t for t in line_taxes if t is not None), Decimal(0)) == tax_total
    )
    single = len(raw) == 1

    def build(gross_of, tax_of) -> list[BillLine]:
        out = []
        for description, account, amount, li_tax, li in raw:
            exact_qty = (
                li is not None
                and li.quantity is not None
                and li.unit_price is not None
                and li.quantity * li.unit_price == amount
            )
            out.append(
                BillLine(
                    description=description,
                    account_erp_id=str(account),
                    gross=gross_of(amount, li_tax),
                    tax=tax_of(amount, li_tax),
                    quantity=li.quantity if exact_qty else None,
                    unit_price=li.unit_price if exact_qty else None,
                )
            )
        return out

    if tax_total == 0:
        if amounts_sum != payload.amount:
            raise BillRefusal(AMOUNT_MISMATCH)
        return BillAllocation(build(lambda a, _t: a, lambda _a, _t: Decimal(0)), tax_total)

    if amounts_sum == payload.amount:
        # Tax-inclusive lines: the gross is the line amount as given.
        if itemised:
            return BillAllocation(build(lambda a, _t: a, lambda _a, t: t), tax_total)
        if single:
            return BillAllocation(build(lambda a, _t: a, lambda _a, _t: tax_total), tax_total)
        return BillAllocation(
            build(lambda a, _t: a, lambda _a, _t: None), tax_total, inclusive_unsplit=True
        )

    if amounts_sum + tax_total == payload.amount:
        # Tax-exclusive lines: the gross is the line amount plus its tax.
        if itemised:
            return BillAllocation(build(lambda a, t: a + t, lambda _a, t: t), tax_total)
        if single:
            return BillAllocation(
                build(lambda a, _t: a + tax_total, lambda _a, _t: tax_total), tax_total
            )
        raise BillRefusal(TAX_NOT_ITEMISED)

    raise BillRefusal(AMOUNT_MISMATCH)
