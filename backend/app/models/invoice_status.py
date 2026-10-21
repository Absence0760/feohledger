"""The invoice status vocabulary and the named groupings of it.

`InvoiceStatus` is the ONE enum for `Invoice.status`: `models.invoice` and
`schemas.invoice` both re-export it from here. It used to be declared twice,
member for member, with nothing holding the two together.

The groupings below are the questions the codebase keeps asking about a
status — "has a human accepted this yet?", "is it still owed?", "is its
content frozen?" — answered once. Each was previously restated as a literal
list in several modules, and the copies had drifted: the budget's realised
spend left out `sending_to_erp`, and the vendor fraud baselines left out
`sending_to_erp`, `sent_to_erp` and `done` (issue #575). A module that needs
one of these sets imports it; `tests/test_invoice_status_groups.py` fails on a
new literal restatement and pins each set against `VALID_TRANSITIONS`.

Pure: no ORM import, so the IO-free analytics module can use it.
"""

from __future__ import annotations

import enum


class InvoiceStatus(enum.StrEnum):
    new = "new"
    pending = "pending"
    ready_for_review = "ready_for_review"
    approved = "approved"
    rejected = "rejected"
    sending_to_erp = "sending_to_erp"
    sent_to_erp = "sent_to_erp"
    posted_in_erp = "posted_in_erp"
    payment_scheduled = "payment_scheduled"
    paid = "paid"
    done = "done"
    failed = "failed"


#: Entered but not yet decided: draft, mid-extraction, or waiting on an
#: approver. The pipeline the cash forecast projects as lower-certainty
#: outflow, the "in review" population of the adaptive baselines, and the
#: statuses a past-due invoice is still worth warning about.
IN_REVIEW_STATUSES: tuple[InvoiceStatus, ...] = (
    InvoiceStatus.new,
    InvoiceStatus.pending,
    InvoiceStatus.ready_for_review,
)

#: Everything before an approval decision has been made and stuck. An invoice
#: here reaches money only through a fresh approval, which is why the
#: bank-change flag leaves it alone and why a PO-match hold may auto-clear
#: against a different PO only here (`invoice_warnings`).
PRE_APPROVAL_STATUSES: frozenset[InvoiceStatus] = frozenset(
    {*IN_REVIEW_STATUSES, InvoiceStatus.rejected}
)

#: Approved and still owed: the firm AP commitment. The AP balance, the aging
#: bands, the committed leg of the cash forecast and the population a
#: vendor-wide discount can still be captured against. `services/analytics`
#: re-exports it as strings (`OPEN_AP_STATUSES`).
OPEN_AP_STATUSES: tuple[InvoiceStatus, ...] = (
    InvoiceStatus.approved,
    InvoiceStatus.sending_to_erp,
    InvoiceStatus.sent_to_erp,
    InvoiceStatus.posted_in_erp,
    InvoiceStatus.payment_scheduled,
)

#: Accepted into the books, whatever has happened since: still owed, paid, or
#: finished. The "historically accepted" population every vendor baseline
#: reads (coding, price and amount history, remit-to address, anomaly
#: detection) and the realised leg of a budget. `paid` / `done` also hold
#: settled history that never went through review (`new -> done`, a CSV import),
#: which is accepted history all the same; where proof of an approval matters,
#: read `Invoice.approval_date` (`services/analytics.TOUCHLESS_*`). `failed` is
#: excluded because it is reachable from `pending` (never reviewed) as well as
#: from `sending_to_erp`, so the status alone proves nothing; the budget adds it
#: back only with the stamp (`budget_service.realised_invoice_clause`). Why the
#: baselines read imported `done` history when §73's touchless rate does not:
#: `docs/decisions.md` §302.
APPROVED_OR_BEYOND_STATUSES: tuple[InvoiceStatus, ...] = (
    *OPEN_AP_STATUSES,
    InvoiceStatus.paid,
    InvoiceStatus.done,
)

#: Past approval AND handed onward (ERP push started, payment scheduled, paid,
#: or finished): the invoice is immutable — no edit, delete, or bulk recode.
#: `approved` is not in it because a reject → rework is still possible there;
#: financial fields are frozen from `approved` by `api/invoices`'
#: `_FINANCIALLY_LOCKED_STATUSES`, which adds it back.
POST_APPROVAL_LOCKED_STATUSES: frozenset[InvoiceStatus] = frozenset(
    set(APPROVED_OR_BEYOND_STATUSES) - {InvoiceStatus.approved}
)
