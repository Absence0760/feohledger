"""The money-path gates every payment and ERP-push path reads.

Which invoice statuses may have money scheduled against them, which exception
types hold a payment run (and which of those also hold the ERP push), and the
refusal a dispatch gives when the invoice row stays locked. They lived in
``api/payments.py``, so every service that applies them (``payment_runs``,
``erp_export_gate``, ``exception_lifecycle``, ``external_payment``) had to
import a router, lazily, to dodge the import cycle. This module owns them; the
routers import them from here.
"""

from app.models.invoice import InvoiceStatus
from app.models.invoice_status import PAYABLE_STATUSES
from app.services.workflow_engine import VALID_TRANSITIONS

# The invoice statuses a payment may be created against — i.e. an invoice that
# has cleared AP approval and can directly transition to `payment_scheduled`.
# `sent_to_erp` is excluded (mid-flight ERP push must reach `posted_in_erp`
# first); `new`/`pending`/`ready_for_review`/`rejected`/`failed` are pre-approval
# and must never have money scheduled against them. This is the single source of
# truth shared by the queue, the run builder, and the standalone payment record,
# so a payment can't be booked against an unapproved invoice on any path.
#
# The set itself is `models/invoice_status.PAYABLE_STATUSES`; this is it as
# strings, for the `.in_()` filters and the `InvoiceStatus(value)` lookups.
PAYABLE_INVOICE_STATUSES = tuple(state.value for state in PAYABLE_STATUSES)

# Of those, the ones that still need the `→ payment_scheduled` transition once a
# payment settles. DERIVED from the state machine rather than restated, so it
# can never name a status `validate_transition` would refuse: a payable status
# is schedulable exactly when `payment_scheduled` is a legal successor.
# (`payment_scheduled` itself is payable — a re-attempt — but is already there,
# and drops out here because it isn't its own successor.) The three dispatch
# legs used to spell this out as a literal that included `sent_to_erp`, which
# `VALID_TRANSITIONS` does NOT allow — and the transition runs AFTER the
# processor accepted the order, so the resulting 409 recorded a settled payment
# as `failed`. `_execute_single_payment` now refuses a non-payable invoice
# before the adapter call; this keeps the two facts from drifting apart again.
SCHEDULABLE_INVOICE_STATUSES = tuple(
    value
    for value in PAYABLE_INVOICE_STATUSES
    if InvoiceStatus.payment_scheduled in VALID_TRANSITIONS.get(InvoiceStatus(value), set())
)

# Exception classes that block an invoice from entering a payment run while
# UNRESOLVED (`open`/`escalated`). Every one of them is an `error`-severity
# financial-integrity flag that approval does NOT gate on — nothing in
# `services/review.py` or `workflow_engine.py` reads warning severity — so
# without this gate each could be approved straight past and paid:
#
#   duplicate            — the same invoice paid a second time
#   fraud_flag           — bank-detail swap, rush payment, stat anomaly, an
#                          altered/never-issued cheque from a Positive Pay return
#   line_total_mismatch  — the header `amount` a run pays openly disagrees with
#                          the invoice's own line items (the header is never
#                          silently recomputed from them — see
#                          `docs/line-total-reconciliation.md`), so paying it
#                          would pay a total the lines don't support
#   payment_reconciliation — the backstop reconciler gave up waiting on a
#                          `submitted` payment past its max age and marked it
#                          `failed`. `failed` is in
#                          `LIVE_PAYMENT_TERMINAL_STATUSES`, so that row stops
#                          holding the invoice's live-payment slot even though
#                          real money may still be in flight at the rail. The
#                          exception is what stops a fresh run paying the same
#                          invoice a second time until a human has reconciled
#                          the rail (see `services/payment_reconciler.py`).
#   quality_hold         — the 4-way leg: the goods failed inspection, were only
#                          partly accepted, or an inspection the match rule
#                          requires is not on record. A four-way match exists so
#                          that failed quality acceptance stops payment; paying
#                          past it pays for goods the business refused.
#   po_mismatch          — paying the invoice would pay more than its purchase
#                          order supports: the cited PO doesn't exist, the
#                          currencies differ (nothing could be compared), it
#                          bills ABOVE the PO beyond the match rule's tolerance,
#                          or it bills beyond the share of the PO actually
#                          received. Only those raise one
#                          (`invoice_warnings._refresh_po_match`): billing LESS
#                          than the PO (a split / blanket PO invoiced delivery by
#                          delivery), an in-tolerance variance and an
#                          over-receipt on its own (decisions §67) stay warnings
#                          on the invoice and never hold it. A refresh that no
#                          longer finds the problem closes the row again. Same
#                          rule as an ERP's price/quantity variance payment
#                          block.
#
# Resolving/dismissing the exception is the human sign-off that clears it. The
# same tuple also scopes segregation of duties on that sign-off
# (`services/exception_lifecycle.segregation_refusal`): someone implicated in
# creating the invoice may not clear a flag that blocks its payment, and on a
# `po_mismatch` / `quality_hold` the invoice's approvers may not either
# (decisions §420).
#
# Order is the precedence a row's `blocked_reason` reports when an invoice
# carries several (`services/payment_runs.blocking_exception_types`).
PAYMENT_BLOCKING_EXCEPTION_TYPES = (
    "duplicate",
    "fraud_flag",
    "line_total_mismatch",
    "payment_reconciliation",
    "quality_hold",
    "po_mismatch",
)

# The payment-blocking types that also refuse the ERP PUSH (decisions §286),
# not just a payment run. Posting a bill records the liability in the ERP's own
# books, and there a bookkeeper can pay it directly (QuickBooks "Mark as paid",
# Bill Pay) without our run gate ever seeing it. So a flag that says the
# liability itself is wrong has to stop the posting:
#
#   duplicate   the obligation is already booked; a second bill is a second
#               liability for one debt.
#   fraud_flag  the obligation may not exist, or not be owed to this payee.
#
# The other four say the liability is real and only its PAYMENT must wait —
# a line-level discrepancy under an approved header amount, a rail that may
# already hold an order, goods awaiting inspection, a bill over its PO or
# receipt. Recording those in the ERP is correct accounting, and refusing it
# would hide a genuine payable from the ledger. Order is the precedence the
# refusal names when an invoice carries both.
#
# The one predicate is `services/payment_runs.blocking_exception_types`, and
# clearing either type is the segregated sign-off of
# `services/exception_lifecycle.segregation_refusal`, because both are members
# of the tuple above (asserted below, so the two lists cannot drift apart).
ERP_EXPORT_BLOCKING_EXCEPTION_TYPES = ("duplicate", "fraud_flag")
assert set(ERP_EXPORT_BLOCKING_EXCEPTION_TYPES) <= set(PAYMENT_BLOCKING_EXCEPTION_TYPES)


#: `Payment.failure_reason` for a dispatch refused because the invoice row was
#: locked past the bound. Retry-safe (`payment_runs._RETRY_SAFE_FAILURE_PREFIXES`).
INVOICE_LOCKED_REASON = "invoice_locked"

#: The 409 body every request path answers with for the same refusal.
INVOICE_LOCKED_DETAIL = (
    "invoice_locked: the invoice is being changed by another request — nothing "
    "was sent to the processor; retry in a moment"
)
