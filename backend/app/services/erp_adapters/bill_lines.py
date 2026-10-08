"""The GL lines a direct adapter posts for an invoice, as ``(account, gross, memo)``.

**Which helper when.** ``bill_allocation.allocate_bill_lines`` is the one
statement of how an invoice's lines become bill lines. Two views of it exist:

* ``bill_allocation.allocate_bill_lines`` — net, tax and gross per line, for the
  ERPs that **derive the bill total from lines plus tax** (Xero, Sage Business
  Cloud Accounting v3.1).
* :func:`bill_lines` (here) — the same lines projected to their tax-inclusive
  gross, for the ERPs that **take tax separately or not at all** (Sage
  Intacct, SYSPRO, Sage Accounting ZA — which splits VAT out of the gross by
  the account's own rate — Blackbaud FE NXT, QuickBooks Online).

Being a projection, it cannot drift from the allocation: a line's amount is its
``total``, else ``quantity * unit_price``, else the bill is refused
(``line_amount_missing``); a coded line posts on its own account's ERP id and is
refused (``account_not_linked``) rather than moved onto the header's; and the
gross lines always sum to exactly ``payload.amount`` — tax-inclusive lines as
given, tax-exclusive lines plus their stated tax — or the bill is refused
(``amount_mismatch`` / ``tax_not_itemised``). There is no fallback that books
the whole amount on the header account when the lines disagree with it: that
would move coded expense to an account the approver never saw.

Not an adapter itself — it registers nothing.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.erp_adapters.base import InvoicePayload
from app.services.erp_adapters.bill_allocation import BillRefusal, allocate_bill_lines


def bill_lines(payload: InvoicePayload) -> list[tuple[str, Decimal, str]] | str:
    """The ``(gl_account_erp_id, gross, memo)`` lines to post, or a refusal reason.

    The grosses sum to exactly ``payload.amount``; the header amount is never
    recomputed from lines. ``memo`` is the line's description, else the
    invoice's, else its number.
    """
    try:
        allocation = allocate_bill_lines(payload)
    except BillRefusal as refusal:
        return refusal.reason
    return [(line.account_erp_id, line.gross, line.description) for line in allocation.lines]
